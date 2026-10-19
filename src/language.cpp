#include "language.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocale>
#include <QSet>

namespace {
constexpr auto pluginId = "lcryolite.clipper";

QString configuredLanguage(const QJsonObject &config)
{
    const auto readEntry = [](const QJsonObject &entry) -> QString {
        if (entry.value(QStringLiteral("id")).toString() != QLatin1String(pluginId))
            return {};
        const QString value = entry.value(QStringLiteral("language")).toString();
        return value == QStringLiteral("system") || value == QStringLiteral("zh")
                || value == QStringLiteral("en") ? value : QString();
    };

    const QJsonObject layout = config.value(QStringLiteral("bar")).toObject()
                                   .value(QStringLiteral("layout")).toObject();
    for (const QString &section : {QStringLiteral("left"), QStringLiteral("centerBegin"),
                                   QStringLiteral("center"), QStringLiteral("centerEnd"),
                                   QStringLiteral("right"), QStringLiteral("tray")}) {
        for (const QJsonValue &value : layout.value(section).toArray()) {
            const QString language = readEntry(value.toObject());
            if (!language.isEmpty())
                return language;
        }
    }
    return QStringLiteral("system");
}
QString systemLanguage()
{
    const QString name = QLocale::system().name().toLower();
    if (name == QStringLiteral("zh") || name.startsWith(QStringLiteral("zh_")))
        return QStringLiteral("zh");
    return QStringLiteral("en");
}

bool retainedLanguage(const QJsonDocument &document, QString *language)
{
    if (!document.isObject())
        return false;
    const QJsonObject root = document.object();
    const QJsonValue version = root.value(QStringLiteral("version"));
    const QJsonValue stored = root.value(QStringLiteral("settings"));
    if (!version.isDouble() || version.toDouble() != 1 || !stored.isObject())
        return false;

    const QJsonObject settings = stored.toObject();
    static const QSet<QString> knownKeys = {
        QStringLiteral("language"), QStringLiteral("previewEnabled"),
        QStringLiteral("previewSide"), QStringLiteral("dataDir"),
        QStringLiteral("executable"),
    };
    for (auto iterator = settings.constBegin(); iterator != settings.constEnd(); ++iterator) {
        if (!knownKeys.contains(iterator.key()))
            return false;
    }

    const QJsonValue languageValue = settings.value(QStringLiteral("language"));
    const QString nextLanguage = languageValue.isUndefined()
        ? QStringLiteral("system") : languageValue.toString();
    if ((!languageValue.isUndefined() && !languageValue.isString())
        || (nextLanguage != QStringLiteral("system") && nextLanguage != QStringLiteral("zh")
            && nextLanguage != QStringLiteral("en")))
        return false;

    const QJsonValue previewEnabled = settings.value(QStringLiteral("previewEnabled"));
    if (!previewEnabled.isUndefined() && !previewEnabled.isBool())
        return false;
    const QJsonValue previewSide = settings.value(QStringLiteral("previewSide"));
    const QString side = previewSide.isUndefined() ? QStringLiteral("right") : previewSide.toString();
    if ((!previewSide.isUndefined() && !previewSide.isString())
        || (side != QStringLiteral("left") && side != QStringLiteral("right")))
        return false;

    const QJsonValue dataDir = settings.value(QStringLiteral("dataDir"));
    const QString directory = dataDir.isUndefined() ? QString() : dataDir.toString();
    if ((!dataDir.isUndefined() && !dataDir.isString()) || directory.contains(QChar::Null)
        || (!directory.isEmpty() && !directory.startsWith(QLatin1Char('/'))))
        return false;
    const QJsonValue executable = settings.value(QStringLiteral("executable"));
    const QString program = executable.isUndefined() ? QStringLiteral("clipper") : executable.toString();
    if ((!executable.isUndefined() && !executable.isString()) || program.contains(QChar::Null)
        || program.trimmed().isEmpty())
        return false;

    *language = nextLanguage;
    return true;
}
}

QString localizedUserMessage(const QString &message)
{
    static constexpr const char *knownMessages[] = {
        "Unknown collection error",
        "Clip not found",
        "A clip cannot be empty",
        "Draft changed; reload it before saving",
        "Draft changed while it was being saved",
        "Captured article bodies cannot be edited",
        "Clip changed; reload it before saving",
        "Only article clips have separate notes",
        "Clip is already in Trash",
        "Clip is not in Trash",
        "Only web links can be opened",
        "Could not open the source browser",
        "Could not open the detail window",
        "Image could not be read or exceeds 12 MiB",
        "Clipboard does not contain a supported image",
        "Image does not belong to this clip",
        "Image is missing",
        "The operation could not be completed",
        "Asset exceeds the 12 MiB limit",
        "Asset data is not valid base64 image data",
        "Asset is not a valid image",
        "Asset image dimensions are unsupported",
        "Asset image format is unsupported",
        "Declared MIME type does not match image data",
        "Could not store the asset",
        "Image capture failed.",
        "Skipped because one capture supports at most 32 images.",
        "Local and private network image URLs are not captured.",
        "Permission is required to retry this public image origin.",
        "Resource is not a supported image type.",
        "Image import has not completed yet.",
        "Image request timed out.",
        "The image download may be truncated.",
        "Image source could not be resolved.",
        "No usable image source was found in the captured scope.",
        "The original post is not available on this forum page. Capture an explicit selection or choose the visible current-page replies scope.",
        "Select visible page content before choosing Save selection.",
        "The rendered repository README could not be identified.",
        "No readable posts were found on the current forum page.",
        "No readable article was found. Save a selection or capture a region instead.",
    };
    for (const char *source : knownMessages) {
        if (message == QLatin1String(source))
            return QCoreApplication::translate("UserMessages", source);
    }
    if (message.startsWith(QStringLiteral("Unknown asset: ")))
        return QCoreApplication::translate("UserMessages", "Unknown asset: %1")
            .arg(message.mid(15));
    if (message.startsWith(QStringLiteral("Could not start ")))
        return QCoreApplication::translate("UserMessages", "Could not start %1")
            .arg(message.mid(16));
    return QCoreApplication::translate("UserMessages", "Details: %1").arg(message);
}

LanguageSettings::LanguageSettings(QObject *parent)
    : QObject(parent)
    , m_settingsPath(QDir(qEnvironmentVariable("XDG_CONFIG_HOME", QDir::homePath() + "/.config"))
                         .filePath(QStringLiteral("omarchy-clipper/settings.json")))
{
    m_refreshTimer.setSingleShot(true);
    m_refreshTimer.setInterval(25);
    connect(&m_refreshTimer, &QTimer::timeout, this, &LanguageSettings::refresh);
    connect(&m_watcher, &QFileSystemWatcher::fileChanged,
            &m_refreshTimer, qOverload<>(&QTimer::start));
    connect(&m_watcher, &QFileSystemWatcher::directoryChanged,
            &m_refreshTimer, qOverload<>(&QTimer::start));
    refresh();
}

void LanguageSettings::watchSettings()
{
    QString directory = QFileInfo(m_settingsPath).absolutePath();
    while (!QFileInfo::exists(directory)) {
        const QString parent = QFileInfo(directory).absolutePath();
        if (parent == directory)
            break;
        directory = parent;
    }
    if (QFileInfo::exists(directory) && !m_watcher.directories().contains(directory))
        m_watcher.addPath(directory);
    if (m_watcher.files().contains(m_settingsPath))
        m_watcher.removePath(m_settingsPath);
    if (QFileInfo::exists(m_settingsPath))
        m_watcher.addPath(m_settingsPath);
    const QString legacy = QDir(qEnvironmentVariable("XDG_CONFIG_HOME", QDir::homePath() + "/.config"))
                               .filePath(QStringLiteral("omarchy/shell.json"));
    if (QFileInfo::exists(legacy) && !m_watcher.files().contains(legacy))
        m_watcher.addPath(legacy);
    const QString legacyDirectory = QFileInfo(legacy).absolutePath();
    if (QFileInfo::exists(legacyDirectory) && !m_watcher.directories().contains(legacyDirectory))
        m_watcher.addPath(legacyDirectory);
}

void LanguageSettings::refresh()
{
    m_error.clear();
    QString next = QStringLiteral("system");
    const bool retained = QFileInfo::exists(m_settingsPath);
    const QString legacy = QDir(qEnvironmentVariable("XDG_CONFIG_HOME", QDir::homePath() + "/.config"))
                               .filePath(QStringLiteral("omarchy/shell.json"));
    QFile file(retained ? m_settingsPath : legacy);
    if (file.open(QIODevice::ReadOnly)) {
        QJsonParseError error;
        const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &error);
        if (error.error == QJsonParseError::NoError && document.isObject()) {
            if (retained) {
                if (!retainedLanguage(document, &next)) {
                    m_error = QStringLiteral("Invalid Clipper settings; repair the saved configuration.");
                    watchSettings();
                    return;
                }
            } else next = configuredLanguage(document.object());
        } else if (retained) {
            m_error = QStringLiteral("Invalid Clipper settings; repair the saved configuration.");
            watchSettings();
            return;
        }
    } else if (retained) {
        m_error = QStringLiteral("Could not read Clipper settings; check configuration permissions.");
        watchSettings();
        return;
    }
    const QString resolved = next == QStringLiteral("system") ? systemLanguage() : next;
    watchSettings();
    if (next == m_language && resolved == m_resolvedLanguage)
        return;
    if (resolved != m_resolvedLanguage)
        installTranslation(resolved);
    m_language = next;
    m_resolvedLanguage = resolved;
    emit languageChanged();
}

void LanguageSettings::installTranslation(const QString &language)
{
    QCoreApplication::removeTranslator(&m_translator);
    if (language != QStringLiteral("zh"))
        return;

    if (m_translator.load(QStringLiteral(":/translations/omarchy-clipper_zh.qm")))
        QCoreApplication::installTranslator(&m_translator);
}
