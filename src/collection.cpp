#include "collection.h"

#include "html.h"

#include <QBuffer>
#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonDocument>
#include <QSaveFile>
#include <QSet>
#include <QSqlDatabase>
#include <QSqlError>
#include <QSqlQuery>
#include <QThread>
#include <QUrl>
#include <QUuid>

#include <stdexcept>

namespace {

class CodedError final : public std::runtime_error {
public:
    CodedError(const QString &message, const QString &code)
        : std::runtime_error(message.toStdString()), code(code) {}
    const QString code;
};

[[noreturn]] void fail(const QString &message, const QString &code = {})
{
    if (!code.isEmpty()) throw CodedError(message, code);
    throw std::runtime_error(message.toStdString());
}

void check(bool condition, const QString &message, const QString &code = {})
{
    if (!condition)
        fail(message, code);
}

QString now()
{
    return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
}

QString newId()
{
    return QUuid::createUuid().toString(QUuid::WithoutBraces);
}

QString compact(const QJsonValue &value)
{
    if (value.isArray())
        return QString::fromUtf8(QJsonDocument(value.toArray()).toJson(QJsonDocument::Compact));
    return QString::fromUtf8(QJsonDocument(value.toObject()).toJson(QJsonDocument::Compact));
}

QJsonArray arrayFromText(const QString &text)
{
    const QJsonDocument document = QJsonDocument::fromJson(text.toUtf8());
    return document.isArray() ? document.array() : QJsonArray();
}

QJsonArray normalizedMissing(const QString &text)
{
    QJsonArray result;
    for (const QJsonValue &value : arrayFromText(text)) {
        if (!value.isObject()) {
            result.append(value);
            continue;
        }
        QJsonObject item = value.toObject();
        QString category = item.value("category").toString();
        if (category.isEmpty()) {
            const QString reason = item.value("reason").toString();
            if (reason == "Permission is required to retry this public image origin.")
                category = QStringLiteral("permission");
            else if (reason.contains("not a supported image type", Qt::CaseInsensitive)
                     || reason == "Asset image dimensions are unsupported"
                     || reason == "Asset image format is unsupported"
                     || reason == "Declared MIME type does not match image data")
                category = QStringLiteral("unsupported");
            else
                category = QStringLiteral("transient");
            item.insert("category", category);
        }
        if (!item.value("retryable").isBool())
            item.insert("retryable", category != "unsupported");
        result.append(item);
    }
    return result;
}

QStringList stringList(const QJsonValue &value, const QString &field)
{
    check(value.isArray(), field + " must be an array");
    QStringList values;
    QSet<QString> seen;
    for (const QJsonValue &entry : value.toArray()) {
        check(entry.isString() && !entry.toString().isEmpty(), field + " contains an invalid ID");
        if (!seen.contains(entry.toString())) {
            seen.insert(entry.toString());
            values.append(entry.toString());
        }
    }
    return values;
}

void executeWithBusyRetry(QSqlQuery &query, const QString &context)
{
    QElapsedTimer timer;
    timer.start();
    int delayMs = 10;
    while (!query.exec()) {
        const QSqlError error = query.lastError();
        const QString code = error.nativeErrorCode();
        const bool busy = code == "5" || code == "6"
            || error.text().contains("database is locked", Qt::CaseInsensitive)
            || error.text().contains("database table is locked", Qt::CaseInsensitive);
        if (!busy)
            fail(context + ": " + error.text());
        if (timer.elapsed() >= 3000)
            fail(context + ": Collection storage stayed busy for 3 seconds; retry the operation");
        QThread::msleep(delayMs);
        delayMs = qMin(delayMs * 2, 100);
    }
}

QJsonObject success(const QJsonValue &result)
{
    return {{"ok", true}, {"result", result}};
}

QString titleFromText(const QString &text, bool hasImages)
{
    for (const QString &line : text.split('\n')) {
        QString clean = line.trimmed();
        while (clean.startsWith('#'))
            clean.remove(0, 1);
        clean = clean.trimmed();
        if (!clean.isEmpty())
            return clean.left(100);
    }
    return hasImages ? QStringLiteral("Image clip") : QStringLiteral("Untitled clip");
}

void validateSourceUrl(const QString &sourceUrl)
{
    const QUrl url(sourceUrl, QUrl::StrictMode);
    check(url.isValid() && (url.scheme() == "http" || url.scheme() == "https")
              && !url.host().isEmpty(),
          "sourceUrl must be an absolute HTTP(S) URL");
}

} // namespace

class Collection::Impl
{
public:
    explicit Impl(const QString &rootPath)
        : root(QDir(rootPath).absolutePath())
        , assetsDir(root + "/assets")
        , connectionName("clipper-" + newId())
    {
        check(QDir().mkpath(root), "Could not create the collection directory");
        check(QDir().mkpath(assetsDir), "Could not create the asset directory");
        db = QSqlDatabase::addDatabase("QSQLITE", connectionName);
        db.setDatabaseName(root + "/collection.sqlite3");
        db.setConnectOptions("QSQLITE_BUSY_TIMEOUT=3000");
        check(db.open(), "Could not open the collection database: " + db.lastError().text());

        QSqlQuery pragmas(db);
        pragmas.prepare("PRAGMA foreign_keys = ON");
        executeWithBusyRetry(pragmas, "Could not enable collection integrity checks");
        pragmas.prepare("PRAGMA journal_mode = WAL");
        executeWithBusyRetry(pragmas, "Could not enable concurrent collection storage");
        pragmas.finish();
        createSchema();
    }

    ~Impl()
    {
        db.close();
        db = QSqlDatabase();
        QSqlDatabase::removeDatabase(connectionName);
    }

    void createSchema()
    {
        const QStringList statements = {
            "CREATE TABLE IF NOT EXISTS draft (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, asset_ids TEXT NOT NULL, revision INTEGER NOT NULL)",
            "INSERT OR IGNORE INTO draft(id,text,asset_ids,revision) VALUES(1,'','[]',0)",
            "CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, mime TEXT NOT NULL, name TEXT NOT NULL, filename TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('mixed','article')), title TEXT NOT NULL, text TEXT NOT NULL, html TEXT NOT NULL, note TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('capturing','incomplete','ready')), missing TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT NOT NULL, revision INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS clip_assets (clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE, asset_id TEXT NOT NULL REFERENCES assets(id), position INTEGER NOT NULL, PRIMARY KEY(clip_id,asset_id))",
            "CREATE TABLE IF NOT EXISTS captures (id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, update_id TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL, html TEXT NOT NULL, text TEXT NOT NULL, missing TEXT NOT NULL, asset_ids TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)"
        };
        for (const QString &statement : statements) {
            QSqlQuery query(db);
            query.prepare(statement);
            executeWithBusyRetry(query, "Could not initialize collection storage");
        }
    }

    QJsonObject draft() const
    {
        QSqlQuery query(db);
        query.prepare("SELECT text,asset_ids,revision FROM draft WHERE id=1");
        executeWithBusyRetry(query, "Could not read the draft");
        check(query.next(), "Draft storage is unavailable");
        const QJsonArray assetIds = arrayFromText(query.value(1).toString());
        QJsonArray assets;
        QSqlQuery assetQuery(db);
        assetQuery.prepare("SELECT mime,name,filename FROM assets WHERE id=?");
        for (const QJsonValue &value : assetIds) {
            const QString id = value.toString();
            assetQuery.bindValue(0, id);
            executeWithBusyRetry(assetQuery, "Could not read draft assets");
            if (assetQuery.next()) {
                assets.append(QJsonObject{{"id", id},
                                          {"url", QUrl::fromLocalFile(assetsDir + "/" + assetQuery.value(2).toString()).toString()},
                                          {"mime", assetQuery.value(0).toString()},
                                          {"name", assetQuery.value(1).toString()}});
            }
            assetQuery.finish();
        }
        return {{"text", query.value(0).toString()},
                {"assetIds", assetIds},
                {"assets", assets},
                {"revision", query.value(2).toInt()}};
    }

    void requireAssets(const QStringList &ids) const
    {
        QSqlQuery query(db);
        query.prepare("SELECT 1 FROM assets WHERE id=?");
        for (const QString &id : ids) {
            query.bindValue(0, id);
            executeWithBusyRetry(query, "Could not validate an asset");
            check(query.next(), "Unknown asset: " + id);
            query.finish();
        }
    }

    QJsonArray assetsForClip(const QString &clipId) const
    {
        QSqlQuery query(db);
        query.prepare("SELECT a.id,a.mime,a.name,a.filename FROM assets a JOIN clip_assets ca ON ca.asset_id=a.id WHERE ca.clip_id=? ORDER BY ca.position");
        query.addBindValue(clipId);
        executeWithBusyRetry(query, "Could not read clip assets");
        QJsonArray result;
        while (query.next()) {
            const QString id = query.value(0).toString();
            result.append(QJsonObject{{"id", id},
                                      {"url", QUrl::fromLocalFile(assetsDir + "/" + query.value(3).toString()).toString()},
                                      {"mime", query.value(1).toString()},
                                      {"name", query.value(2).toString()}});
        }
        return result;
    }

    QJsonObject pendingCaptureForClip(const QString &clipId) const
    {
        QSqlQuery query(db);
        query.prepare("SELECT * FROM captures WHERE clip_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1");
        query.addBindValue(clipId);
        executeWithBusyRetry(query, "Could not read pending capture metadata");
        if (!query.next())
            return {};
        const QString storedStatus = query.value("status").toString();
        return {{"captureId", query.value("id").toString()},
                {"status", storedStatus == "pending" ? "pending" : "incomplete"},
                {"reason", query.value("reason").toString()},
                {"missing", normalizedMissing(query.value("missing").toString())},
                {"isUpdate", !query.value("update_id").toString().isEmpty()},
                {"retryable", true},
                {"sourceUrl", query.value("source_url").toString()},
                {"scope", query.value("scope").toString()},
                {"createdAt", query.value("created_at").toString()}};
    }

    QJsonObject clipFromCurrent(const QSqlQuery &query, bool includeHtml) const
    {
        QJsonObject clip{{"id", query.value("id").toString()},
                         {"kind", query.value("kind").toString()},
                         {"title", query.value("title").toString()},
                         {"text", query.value("text").toString()},
                         {"note", query.value("note").toString()},
                         {"assets", assetsForClip(query.value("id").toString())},
                         {"sourceUrl", query.value("source_url").toString()},
                         {"scope", query.value("scope").toString()},
                         {"status", query.value("status").toString()},
                         {"missing", normalizedMissing(query.value("missing").toString())},
                         {"createdAt", query.value("created_at").toString()},
                         {"deletedAt", query.value("deleted_at").toString()},
                         {"revision", query.value("revision").toInt()}};
        if (includeHtml)
            clip.insert("html", query.value("html").toString());
        const QJsonObject pendingCapture = pendingCaptureForClip(query.value("id").toString());
        if (!pendingCapture.isEmpty())
            clip.insert("pendingCapture", pendingCapture);
        return clip;
    }

    QJsonObject getClip(const QString &id, bool includeHtml = true) const
    {
        QSqlQuery query(db);
        query.prepare("SELECT * FROM clips WHERE id=?");
        query.addBindValue(id);
        executeWithBusyRetry(query, "Could not read the clip");
        check(query.next(), "Clip not found");
        return clipFromCurrent(query, includeHtml);
    }

    void linkAssets(const QString &clipId, const QStringList &ids)
    {
        QSqlQuery query(db);
        query.prepare("INSERT INTO clip_assets(clip_id,asset_id,position) VALUES(?,?,?)");
        for (int index = 0; index < ids.size(); ++index) {
            query.bindValue(0, clipId);
            query.bindValue(1, ids.at(index));
            query.bindValue(2, index);
            executeWithBusyRetry(query, "Could not attach an asset");
            query.finish();
        }
    }

    QString insertMixed(const QString &text, const QStringList &assetIds, const QString &title,
                        const QString &sourceUrl, const QString &scope)
    {
        requireAssets(assetIds);
        check(!text.trimmed().isEmpty() || !assetIds.isEmpty(), "A clip cannot be empty");
        const QString id = newId();
        QSqlQuery query(db);
        query.prepare("INSERT INTO clips(id,kind,title,text,html,note,source_url,scope,status,missing,created_at,deleted_at,revision) VALUES(?, 'mixed', ?, ?, '', '', ?, ?, 'ready', '[]', ?, '', 1)");
        query.addBindValue(id);
        query.addBindValue(title.isEmpty() ? titleFromText(text, !assetIds.isEmpty()) : title.left(200));
        query.addBindValue(text);
        query.addBindValue(sourceUrl.isEmpty() ? QStringLiteral("") : sourceUrl);
        query.addBindValue(scope.isEmpty() ? QStringLiteral("") : scope);
        query.addBindValue(now());
        executeWithBusyRetry(query, "Could not save the mixed clip");
        linkAssets(id, assetIds);
        return id;
    }

    void beginTransaction()
    {
        QSqlQuery query(db);
        query.prepare("BEGIN IMMEDIATE");
        executeWithBusyRetry(query, "Could not start a storage transaction");
    }

    void commitTransaction()
    {
        QSqlQuery query(db);
        query.prepare("COMMIT");
        executeWithBusyRetry(query, "Could not commit storage changes");
    }

    void cleanupOrphanAssets(const QSet<QString> &candidates)
    {
        if (candidates.isEmpty())
            return;
        QSet<QString> protectedIds;
        for (const QJsonValue &value : draft().value("assetIds").toArray())
            protectedIds.insert(value.toString());
        QSqlQuery captures(db);
        captures.prepare("SELECT asset_ids FROM captures");
        executeWithBusyRetry(captures, "Could not inspect pending capture assets");
        while (captures.next()) {
            for (const QJsonValue &value : arrayFromText(captures.value(0).toString()))
                protectedIds.insert(value.toString());
        }

        QList<QPair<QString, QString>> unused;
        QSqlQuery query(db);
        query.prepare("SELECT a.filename FROM assets a WHERE a.id=? AND NOT EXISTS(SELECT 1 FROM clip_assets ca WHERE ca.asset_id=a.id)");
        for (const QString &id : candidates) {
            if (protectedIds.contains(id))
                continue;
            query.bindValue(0, id);
            executeWithBusyRetry(query, "Could not inspect an unused asset");
            if (query.next())
                unused.append({id, query.value(0).toString()});
            query.finish();
        }
        for (const auto &entry : unused) {
            QSqlQuery remove(db);
            remove.prepare("DELETE FROM assets WHERE id=?");
            remove.addBindValue(entry.first);
            executeWithBusyRetry(remove, "Could not remove unused asset metadata");
            QFile::remove(assetsDir + "/" + entry.second);
        }
    }

    QString root;
    QString assetsDir;
    QString connectionName;
    QSqlDatabase db;
};

Collection::Collection(const QString &root)
    : d(std::make_unique<Impl>(root))
{
}

Collection::~Collection() = default;

QString Collection::assetPath(const QString &id) const
{
    static const QRegularExpression validId("^[A-Za-z0-9_-]{1,128}$");
    if (!validId.match(id).hasMatch())
        return {};
    QSqlQuery query(d->db);
    query.prepare("SELECT filename FROM assets WHERE id=?");
    query.addBindValue(id);
    if (!query.exec() || !query.next())
        return {};
    const QString filename = query.value(0).toString();
    const QString path = QDir(d->assetsDir).absoluteFilePath(filename);
    const QString canonicalDir = QFileInfo(d->assetsDir).canonicalFilePath();
    const QString canonicalPath = QFileInfo(path).canonicalFilePath();
    if (canonicalDir.isEmpty() || canonicalPath.isEmpty()
        || !canonicalPath.startsWith(canonicalDir + QDir::separator()))
        return {};
    return canonicalPath;
}

QJsonObject Collection::dispatch(const QJsonObject &request)
{
    try {
        const QString op = request.value("op").toString();
        check(!op.isEmpty(), "Missing operation");

        if (op == "draft.get")
            return success(d->draft());

        if (op == "draft.save") {
            check(request.value("text").isString(), "text must be a string");
            check(request.value("expectedRevision").isDouble(), "expectedRevision is required");
            const QStringList ids = stringList(request.value("assetIds"), "assetIds");
            d->requireAssets(ids);
            QSet<QString> removedAssetIds;
            for (const QJsonValue &value : d->draft().value("assetIds").toArray())
                removedAssetIds.insert(value.toString());
            for (const QString &id : ids)
                removedAssetIds.remove(id);
            QSqlQuery query(d->db);
            query.prepare("UPDATE draft SET text=?,asset_ids=?,revision=revision+1 WHERE id=1 AND revision=?");
            query.addBindValue(request.value("text").toString());
            query.addBindValue(compact(QJsonArray::fromStringList(ids)));
            query.addBindValue(request.value("expectedRevision").toInt());
            executeWithBusyRetry(query, "Could not save the draft");
            check(query.numRowsAffected() == 1, "Draft changed; reload it before saving");
            d->cleanupOrphanAssets(removedAssetIds);
            return success(d->draft());
        }

        if (op == "draft.commit") {
            check(request.value("expectedRevision").isDouble(), "expectedRevision is required");
            const QJsonObject draft = d->draft();
            check(draft.value("revision").toInt() == request.value("expectedRevision").toInt(),
                  "Draft changed; reload it before saving");
            const QString text = draft.value("text").toString();
            const QStringList ids = stringList(draft.value("assetIds"), "assetIds");
            d->beginTransaction();
            try {
                const QString id = d->insertMixed(text, ids, {}, {}, {});
                QSqlQuery clear(d->db);
                clear.prepare("UPDATE draft SET text='',asset_ids='[]',revision=revision+1 WHERE id=1 AND revision=?");
                clear.addBindValue(request.value("expectedRevision").toInt());
                executeWithBusyRetry(clear, "Could not clear the saved draft");
                check(clear.numRowsAffected() == 1, "Draft changed while it was being saved");
                d->commitTransaction();
                return success(d->getClip(id));
            } catch (...) {
                d->db.rollback();
                throw;
            }
        }

        if (op == "asset.import") {
            check(request.value("data").isString(), "data must be base64 text");
            check(request.value("mime").isString(), "mime is required");
            const QByteArray encoded = request.value("data").toString().toLatin1();
            check(encoded.size() <= 16 * 1024 * 1024 + 16, "Asset exceeds the 12 MiB limit");
            const QByteArray bytes = QByteArray::fromBase64(encoded, QByteArray::AbortOnBase64DecodingErrors);
            check(!bytes.isNull() && !bytes.isEmpty(), "Asset data is not valid base64 image data");
            check(bytes.size() <= 12 * 1024 * 1024, "Asset exceeds the 12 MiB limit");
            QBuffer buffer;
            buffer.setData(bytes);
            check(buffer.open(QIODevice::ReadOnly), "Could not inspect asset data");
            QImageReader reader(&buffer);
            reader.setDecideFormatFromContent(true);
            const QByteArray format = reader.format().toLower();
            const QImage image = reader.read();
            check(!image.isNull(), "Asset is not a valid image", "IMAGE_DECODE_FAILED");
            check(image.width() > 0 && image.height() > 0
                      && qint64(image.width()) * qint64(image.height()) <= 100000000,
                  "Asset image dimensions are unsupported", "IMAGE_DIMENSIONS_UNSUPPORTED");

            QString mime;
            QString extension;
            if (format == "png") { mime = "image/png"; extension = "png"; }
            else if (format == "jpeg" || format == "jpg") { mime = "image/jpeg"; extension = "jpg"; }
            else if (format == "gif") { mime = "image/gif"; extension = "gif"; }
            else if (format == "webp") { mime = "image/webp"; extension = "webp"; }
            else if (format == "bmp") { mime = "image/bmp"; extension = "bmp"; }
            else fail("Asset image format is unsupported", "IMAGE_FORMAT_UNSUPPORTED");
            QString declared = request.value("mime").toString().trimmed().toLower();
            if (declared == "image/jpg")
                declared = "image/jpeg";
            check(declared == mime, "Declared MIME type does not match image data", "IMAGE_MIME_MISMATCH");

            const QString id = newId();
            const QString filename = id + "." + extension;
            QSaveFile file(d->assetsDir + "/" + filename);
            check(file.open(QIODevice::WriteOnly) && file.write(bytes) == bytes.size() && file.commit(),
                  "Could not store the asset");
            QSqlQuery query(d->db);
            query.prepare("INSERT INTO assets(id,mime,name,filename,created_at) VALUES(?,?,?,?,?)");
            query.addBindValue(id);
            query.addBindValue(mime);
            const QString name = request.value("name").toString().trimmed();
            query.addBindValue(name.isEmpty() ? filename : QFileInfo(name).fileName().left(255));
            query.addBindValue(filename);
            query.addBindValue(now());
            try {
                executeWithBusyRetry(query, "Could not store asset metadata");
            } catch (...) {
                QFile::remove(d->assetsDir + "/" + filename);
                throw;
            }
            return success(QJsonObject{{"id", id},
                                       {"url", QUrl::fromLocalFile(d->assetsDir + "/" + filename).toString()},
                                       {"mime", mime},
                                       {"name", name.isEmpty() ? filename : QFileInfo(name).fileName().left(255)}});
        }

        if (op == "clip.list") {
            const QString filter = request.value("filter").toString("all");
            check(filter == "all" || filter == "articles" || filter == "notes" || filter == "images",
                  "Unknown clip filter");
            const bool trash = request.value("trash").toBool(false);
            const QString queryText = request.value("query").toString();
            const int offset = qBound(0, request.value("offset").toInt(0), 1000000);
            const int limit = qBound(1, request.value("limit").toInt(100), 500);
            QString where = trash ? "deleted_at<>''" : "deleted_at=''";
            if (filter == "articles")
                where += " AND kind='article'";
            else if (filter == "notes")
                where += " AND kind='mixed' AND trim(text)<>''";
            else if (filter == "images")
                where += " AND kind='mixed' AND trim(text)='' AND EXISTS(SELECT 1 FROM clip_assets ca WHERE ca.clip_id=clips.id)";
            if (!queryText.isEmpty())
                where += " AND (title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' OR html LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\')";
            QString pattern = queryText;
            pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_");
            pattern = "%" + pattern + "%";

            QSqlQuery count(d->db);
            count.prepare("SELECT count(*) FROM clips WHERE " + where);
            if (!queryText.isEmpty())
                for (int i = 0; i < 4; ++i) count.addBindValue(pattern);
            executeWithBusyRetry(count, "Could not count clips");
            count.next();
            const int total = count.value(0).toInt();

            QSqlQuery listing(d->db);
            listing.prepare("SELECT * FROM clips WHERE " + where + " ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?");
            if (!queryText.isEmpty())
                for (int i = 0; i < 4; ++i) listing.addBindValue(pattern);
            listing.addBindValue(limit);
            listing.addBindValue(offset);
            executeWithBusyRetry(listing, "Could not list clips");
            QJsonArray items;
            while (listing.next())
                items.append(d->clipFromCurrent(listing, false));
            return success(QJsonObject{{"items", items}, {"total", total}});
        }

        if (op == "clip.get")
            return success(d->getClip(request.value("id").toString()));

        if (op == "clip.edit") {
            const QString id = request.value("id").toString();
            const QJsonObject old = d->getClip(id);
            QSet<QString> replacedAssetIds;
            for (const QJsonValue &value : old.value("assets").toArray())
                replacedAssetIds.insert(value.toObject().value("id").toString());
            check(old.value("kind") == "mixed", "Captured article bodies cannot be edited");
            check(request.value("text").isString(), "text must be a string");
            const QStringList ids = stringList(request.value("assetIds"), "assetIds");
            d->requireAssets(ids);
            check(!request.value("text").toString().trimmed().isEmpty() || !ids.isEmpty(),
                  "A clip cannot be empty");
            d->beginTransaction();
            try {
                QSqlQuery update(d->db);
                update.prepare("UPDATE clips SET text=?,title=?,revision=revision+1 WHERE id=? AND revision=?");
                update.addBindValue(request.value("text").toString());
                update.addBindValue(titleFromText(request.value("text").toString(), !ids.isEmpty()));
                update.addBindValue(id);
                update.addBindValue(request.value("expectedRevision").toInt(-1));
                executeWithBusyRetry(update, "Could not edit the clip");
                check(update.numRowsAffected() == 1, "Clip changed; reload it before saving");
                QSqlQuery clear(d->db);
                clear.prepare("DELETE FROM clip_assets WHERE clip_id=?");
                clear.addBindValue(id);
                executeWithBusyRetry(clear, "Could not replace clip assets");
                d->linkAssets(id, ids);
                d->commitTransaction();
            } catch (...) {
                d->db.rollback();
                throw;
            }
            d->cleanupOrphanAssets(replacedAssetIds);
            return success(d->getClip(id));
        }

        if (op == "note.save") {
            const QString id = request.value("id").toString();
            const QJsonObject old = d->getClip(id);
            check(old.value("kind") == "article", "Only article clips have separate notes");
            check(request.value("note").isString(), "note must be a string");
            QSqlQuery update(d->db);
            update.prepare("UPDATE clips SET note=?,revision=revision+1 WHERE id=? AND revision=?");
            update.addBindValue(request.value("note").toString());
            update.addBindValue(id);
            update.addBindValue(request.value("expectedRevision").toInt(-1));
            executeWithBusyRetry(update, "Could not save the article note");
            check(update.numRowsAffected() == 1, "Clip changed; reload it before saving");
            return success(d->getClip(id));
        }

        if (op == "capture.check") {
            const QString sourceUrl = request.value("sourceUrl").toString();
            const QString scope = request.value("scope").toString();
            validateSourceUrl(sourceUrl);
            check(!scope.trimmed().isEmpty(), "scope is required");
            QSqlQuery query(d->db);
            query.prepare("SELECT * FROM clips WHERE kind='article' AND source_url=? AND scope=? ORDER BY (deleted_at='') DESC,created_at DESC LIMIT 1");
            query.addBindValue(sourceUrl);
            query.addBindValue(scope);
            executeWithBusyRetry(query, "Could not check for an existing capture");
            return success(QJsonObject{{"existing", query.next() ? QJsonValue(d->clipFromCurrent(query, false)) : QJsonValue(QJsonValue::Null)}});
        }

        if (op == "capture.begin") {
            const QString title = request.value("title").toString().trimmed();
            const QString sourceUrl = request.value("sourceUrl").toString();
            const QString scope = request.value("scope").toString().trimmed();
            validateSourceUrl(sourceUrl);
            check(!title.isEmpty(), "title is required");
            check(!scope.isEmpty(), "scope is required");
            check(request.value("missing").isArray(), "missing must be an array");
            const QString sanitized = sanitizeCapturedHtml(request.value("html").toString(), {});
            const QString captureId = newId();
            QString updateId = request.value("updateId").toString();
            if (updateId.isNull())
                updateId = QStringLiteral("");
            QString clipId = updateId;
            if (!updateId.isEmpty()) {
                const QJsonObject existing = d->getClip(updateId);
                check(existing.value("kind") == "article" && existing.value("deletedAt").toString().isEmpty(),
                      "Update target must be an active article clip");
            }
            QSet<QString> supersededAssetIds;
            QJsonObject existingCapture;
            d->beginTransaction();
            try {
                if (updateId.isEmpty()) {
                    QSqlQuery duplicate(d->db);
                    duplicate.prepare("SELECT * FROM clips WHERE kind='article' AND source_url=? AND scope=? AND deleted_at='' ORDER BY created_at DESC,rowid DESC LIMIT 1");
                    duplicate.addBindValue(sourceUrl);
                    duplicate.addBindValue(scope);
                    executeWithBusyRetry(duplicate, "Could not check for a concurrent capture");
                    if (duplicate.next()) {
                        existingCapture = d->clipFromCurrent(duplicate, false);
                    } else {
                        clipId = newId();
                        QSqlQuery insert(d->db);
                        insert.prepare("INSERT INTO clips(id,kind,title,text,html,note,source_url,scope,status,missing,created_at,deleted_at,revision) VALUES(?,'article',?,?,?,?,?,?,'capturing',?,?, '',1)");
                        insert.addBindValue(clipId);
                        insert.addBindValue(title.left(200));
                        insert.addBindValue(request.value("text").toString(QStringLiteral("")));
                        insert.addBindValue(sanitized);
                        insert.addBindValue(QStringLiteral(""));
                        insert.addBindValue(sourceUrl);
                        insert.addBindValue(scope);
                        insert.addBindValue(compact(request.value("missing")));
                        insert.addBindValue(now());
                        executeWithBusyRetry(insert, "Could not begin the capture");
                    }
                } else {
                    QSqlQuery previous(d->db);
                    previous.prepare("SELECT asset_ids FROM captures WHERE clip_id=?");
                    previous.addBindValue(clipId);
                    executeWithBusyRetry(previous, "Could not inspect an earlier capture attempt");
                    while (previous.next()) {
                        for (const QJsonValue &value : arrayFromText(previous.value(0).toString()))
                            supersededAssetIds.insert(value.toString());
                    }
                    QSqlQuery supersede(d->db);
                    supersede.prepare("DELETE FROM captures WHERE clip_id=?");
                    supersede.addBindValue(clipId);
                    executeWithBusyRetry(supersede, "Could not supersede an earlier capture attempt");
                }
                if (existingCapture.isEmpty()) {
                    QSqlQuery capture(d->db);
                    capture.prepare("INSERT INTO captures(id,clip_id,update_id,title,source_url,scope,html,text,missing,asset_ids,status,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,'[]','pending','',?)");
                    capture.addBindValue(captureId);
                    capture.addBindValue(clipId);
                    capture.addBindValue(updateId);
                    capture.addBindValue(title.left(200));
                    capture.addBindValue(sourceUrl);
                    capture.addBindValue(scope);
                    capture.addBindValue(sanitized);
                    capture.addBindValue(request.value("text").toString(QStringLiteral("")));
                    capture.addBindValue(compact(request.value("missing")));
                    capture.addBindValue(now());
                    executeWithBusyRetry(capture, "Could not persist capture progress");
                }
                d->commitTransaction();
            } catch (...) {
                d->db.rollback();
                throw;
            }
            d->cleanupOrphanAssets(supersededAssetIds);
            if (!existingCapture.isEmpty())
                return success(QJsonObject{{"existing", existingCapture}});
            return success(QJsonObject{{"captureId", captureId}, {"clipId", clipId}});
        }

        if (op == "capture.finish") {
            const QString captureId = request.value("captureId").toString();
            const QStringList ids = stringList(request.value("assetIds"), "assetIds");
            d->requireAssets(ids);
            check(request.value("missing").isArray(), "missing must be an array");
            const QString sanitized = sanitizeCapturedHtml(request.value("html").toString(), QSet<QString>(ids.begin(), ids.end()));
            QSqlQuery capture(d->db);
            capture.prepare("SELECT * FROM captures WHERE id=?");
            capture.addBindValue(captureId);
            executeWithBusyRetry(capture, "Could not read capture progress");
            check(capture.next(), "Capture not found");
            const QString clipId = capture.value("clip_id").toString();
            const bool isUpdate = !capture.value("update_id").toString().isEmpty();
            const QString capturedTitle = capture.value("title").toString();
            const QString capturedSourceUrl = capture.value("source_url").toString();
            const QString capturedScope = capture.value("scope").toString();
            capture.finish();
            const bool incomplete = !request.value("missing").toArray().isEmpty();
            const QString status = incomplete ? "incomplete" : "ready";
            QSet<QString> replacedAssetIds;
            for (const QJsonValue &value : d->getClip(clipId).value("assets").toArray())
                replacedAssetIds.insert(value.toObject().value("id").toString());
            check(!request.value("text").toString().trimmed().isEmpty() || !sanitized.trimmed().isEmpty() || !ids.isEmpty(),
                  "Captured content cannot be empty");
            d->beginTransaction();
            try {
                if (!(isUpdate && incomplete)) {
                    QSqlQuery update(d->db);
                    update.prepare("UPDATE clips SET title=?,text=?,html=?,source_url=?,scope=?,status=?,missing=?,created_at=?,revision=revision+1 WHERE id=?");
                    update.addBindValue(capturedTitle);
                    update.addBindValue(request.value("text").toString());
                    update.addBindValue(sanitized);
                    update.addBindValue(capturedSourceUrl);
                    update.addBindValue(capturedScope);
                    update.addBindValue(status);
                    update.addBindValue(compact(request.value("missing")));
                    update.addBindValue(now());
                    update.addBindValue(clipId);
                    executeWithBusyRetry(update, "Could not finish the capture");
                    check(update.numRowsAffected() == 1, "Capture target no longer exists");
                    QSqlQuery clear(d->db);
                    clear.prepare("DELETE FROM clip_assets WHERE clip_id=?");
                    clear.addBindValue(clipId);
                    executeWithBusyRetry(clear, "Could not replace captured assets");
                    d->linkAssets(clipId, ids);
                }
                if (incomplete) {
                    QSqlQuery retain(d->db);
                    retain.prepare("UPDATE captures SET html=?,text=?,missing=?,asset_ids=?,status='incomplete',reason='' WHERE id=?");
                    retain.addBindValue(sanitized);
                    retain.addBindValue(request.value("text").toString());
                    retain.addBindValue(compact(request.value("missing")));
                    retain.addBindValue(compact(QJsonArray::fromStringList(ids)));
                    retain.addBindValue(captureId);
                    executeWithBusyRetry(retain, "Could not retain incomplete capture progress");
                } else {
                    QSqlQuery done(d->db);
                    done.prepare("DELETE FROM captures WHERE id=?");
                    done.addBindValue(captureId);
                    executeWithBusyRetry(done, "Could not complete capture progress");
                }
                d->commitTransaction();
            } catch (...) {
                d->db.rollback();
                throw;
            }
            if (!(isUpdate && incomplete))
                d->cleanupOrphanAssets(replacedAssetIds);
            QJsonObject result = d->getClip(clipId);
            if (isUpdate && incomplete) {
                result.insert("updateStatus", "incomplete");
                result.insert("captureId", captureId);
            }
            return success(result);
        }

        if (op == "capture.abort") {
            const QString captureId = request.value("captureId").toString();
            const QString reason = request.value("reason").toString().trimmed();
            check(!reason.isEmpty(), "reason is required");
            QSqlQuery lookup(d->db);
            lookup.prepare("SELECT clip_id,update_id,missing FROM captures WHERE id=?");
            lookup.addBindValue(captureId);
            executeWithBusyRetry(lookup, "Could not read capture progress");
            check(lookup.next(), "Capture not found");
            const QString clipId = lookup.value("clip_id").toString();
            const bool isUpdate = !lookup.value("update_id").toString().isEmpty();
            const QJsonArray storedMissing = arrayFromText(lookup.value("missing").toString());
            lookup.finish();
            d->beginTransaction();
            try {
                QSqlQuery update(d->db);
                update.prepare("UPDATE captures SET status='aborted',reason=? WHERE id=?");
                update.addBindValue(reason.left(1000));
                update.addBindValue(captureId);
                executeWithBusyRetry(update, "Could not retain failed capture progress");
                if (!isUpdate) {
                    QJsonArray missing = storedMissing;
                    missing.append(QJsonObject{{"url", ""}, {"reason", reason.left(1000)}});
                    QSqlQuery mark(d->db);
                    mark.prepare("UPDATE clips SET status='incomplete',missing=?,revision=revision+1 WHERE id=?");
                    mark.addBindValue(compact(missing));
                    mark.addBindValue(clipId);
                    executeWithBusyRetry(mark, "Could not mark the incomplete capture");
                }
                d->commitTransaction();
            } catch (...) {
                d->db.rollback();
                throw;
            }
            return success(QJsonObject{{"captureId", captureId}, {"status", "incomplete"}, {"reason", reason.left(1000)}});
        }

        if (op == "capture.pending") {
            QSqlQuery query(d->db);
            query.prepare("SELECT * FROM captures ORDER BY created_at");
            executeWithBusyRetry(query, "Could not list pending captures");
            QJsonArray items;
            while (query.next()) {
                items.append(QJsonObject{{"captureId", query.value("id").toString()},
                                         {"clipId", query.value("clip_id").toString()},
                                         {"updateId", query.value("update_id").toString()},
                                         {"title", query.value("title").toString()},
                                         {"sourceUrl", query.value("source_url").toString()},
                                         {"scope", query.value("scope").toString()},
                                         {"status", query.value("status").toString() == "pending" ? "capturing" : "incomplete"},
                                         {"missing", normalizedMissing(query.value("missing").toString())},
                                         {"reason", query.value("reason").toString()},
                                         {"createdAt", query.value("created_at").toString()}});
            }
            return success(QJsonObject{{"items", items}});
        }

        if (op == "capture.mixed") {
            const QString sourceUrl = request.value("sourceUrl").toString();
            if (!sourceUrl.isEmpty())
                validateSourceUrl(sourceUrl);
            const QString scope = request.value("scope").toString().trimmed();
            if (!scope.isEmpty()) {
                check(!sourceUrl.isEmpty(), "scope requires a sourceUrl");
                static const QRegularExpression validScope("^[a-z][a-z0-9-]{0,63}$");
                check(validScope.match(scope).hasMatch(), "scope is invalid");
            }
            const QStringList ids = stringList(request.value("assetIds"), "assetIds");
            d->beginTransaction();
            try {
                const QString id = d->insertMixed(request.value("text").toString(), ids,
                                                  request.value("title").toString(), sourceUrl, scope);
                d->commitTransaction();
                return success(d->getClip(id));
            } catch (...) {
                d->db.rollback();
                throw;
            }
        }

        if (op == "clip.trash" || op == "clip.restore") {
            const QString id = request.value("id").toString();
            d->getClip(id);
            QSqlQuery query(d->db);
            query.prepare(op == "clip.trash"
                              ? "UPDATE clips SET deleted_at=?,revision=revision+1 WHERE id=? AND deleted_at=''"
                              : "UPDATE clips SET deleted_at='',revision=revision+1 WHERE id=? AND deleted_at<>''");
            if (op == "clip.trash")
                query.addBindValue(now());
            query.addBindValue(id);
            executeWithBusyRetry(query, "Could not change clip lifecycle");
            check(query.numRowsAffected() == 1, op == "clip.trash" ? "Clip is already in Trash" : "Clip is not in Trash");
            return success(d->getClip(id));
        }

        if (op == "trash.empty") {
            check(request.value("confirm").isBool() && request.value("confirm").toBool(),
                  "Emptying Trash requires confirm: true");
            QSet<QString> removedAssetIds;
            int removed = 0;
            d->beginTransaction();
            try {
                QSqlQuery assets(d->db);
                assets.prepare("SELECT DISTINCT ca.asset_id FROM clip_assets ca JOIN clips c ON c.id=ca.clip_id WHERE c.deleted_at<>''");
                executeWithBusyRetry(assets, "Could not inspect Trash assets");
                while (assets.next())
                    removedAssetIds.insert(assets.value(0).toString());

                QSqlQuery captureAssets(d->db);
                captureAssets.prepare("SELECT cp.asset_ids FROM captures cp JOIN clips c ON c.id=cp.clip_id WHERE c.deleted_at<>''");
                executeWithBusyRetry(captureAssets, "Could not inspect Trash capture assets");
                while (captureAssets.next()) {
                    for (const QJsonValue &value : arrayFromText(captureAssets.value(0).toString()))
                        removedAssetIds.insert(value.toString());
                }

                QSqlQuery captures(d->db);
                captures.prepare("DELETE FROM captures WHERE clip_id IN (SELECT id FROM clips WHERE deleted_at<>'')");
                executeWithBusyRetry(captures, "Could not remove Trash capture progress");

                QSqlQuery clips(d->db);
                clips.prepare("DELETE FROM clips WHERE deleted_at<>''");
                executeWithBusyRetry(clips, "Could not empty Trash");
                removed = clips.numRowsAffected();
                d->commitTransaction();
            } catch (...) {
                d->db.rollback();
                throw;
            }
            d->cleanupOrphanAssets(removedAssetIds);
            return success(QJsonObject{{"removed", removed}});
        }

        fail("Unknown operation: " + op);
    } catch (const CodedError &error) {
        return {{"ok", false}, {"error", QString::fromUtf8(error.what())}, {"errorCode", error.code}};
    } catch (const std::exception &error) {
        return {{"ok", false}, {"error", QString::fromUtf8(error.what())}};
    }
}
