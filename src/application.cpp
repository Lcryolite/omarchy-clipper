#include "application.h"
#include "html.h"
#include "theme.h"

#include <QBuffer>
#include <QCoreApplication>
#include <QDateTime>
#include <QDesktopServices>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QMimeDatabase>
#include <QProcess>
#include <QProcessEnvironment>
#include <QQuickWebEngineProfile>
#include <QTextDocument>
#include <QTemporaryDir>
#include <QUrl>
#include <QWebEngineUrlRequestInfo>
#include <QWebEngineUrlRequestInterceptor>
#include <QWebEngineUrlRequestJob>
#include <QWebEngineUrlScheme>
#include <QWebEngineUrlSchemeHandler>

namespace {
QJsonObject success(const QJsonValue &value) { return {{"ok", true}, {"result", value}}; }
QJsonObject failure(const QString &message) { return {{"ok", false}, {"error", message}}; }

bool webUrl(const QString &text) {
    const QUrl url(text);
    return url.isValid() && !url.host().isEmpty() && url.userInfo().isEmpty()
        && (url.scheme() == "https" || url.scheme() == "http");
}

QByteArray runProcess(const QString &program, const QStringList &args, const QByteArray &input = {}, int timeout = 10000,
                      const QProcessEnvironment &environment = QProcessEnvironment::systemEnvironment()) {
    QProcess process;
    process.setProcessEnvironment(environment);
    process.start(program, args);
    if (!process.waitForStarted(3000)) throw QString("Could not start %1").arg(program);
    if (!input.isEmpty()) process.write(input);
    process.closeWriteChannel();
    if (!process.waitForFinished(timeout)) {
        process.kill();
        process.waitForFinished();
        throw QString("%1 timed out").arg(program);
    }
    if (process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0)
        throw QString("%1 was cancelled or failed").arg(program);
    return process.readAllStandardOutput();
}

class LocalRequests final : public QWebEngineUrlRequestInterceptor {
public:
    using QWebEngineUrlRequestInterceptor::QWebEngineUrlRequestInterceptor;
    void interceptRequest(QWebEngineUrlRequestInfo &info) override {
        const auto scheme = info.requestUrl().scheme();
        if (scheme != "clipper" && info.requestUrl() != QUrl("about:blank")) info.block(true);
    }
};

class ReaderResources final : public QWebEngineUrlSchemeHandler {
public:
    explicit ReaderResources(Application &app) : QWebEngineUrlSchemeHandler(&app), app(app) {}
    void requestStarted(QWebEngineUrlRequestJob *job) override {
        try {
            serve(job);
        } catch (const std::exception &) {
            job->fail(QWebEngineUrlRequestJob::RequestFailed);
        }
    }
    void serve(QWebEngineUrlRequestJob *job) {
        if (job->requestMethod() != "GET") { job->fail(QWebEngineUrlRequestJob::RequestDenied); return; }
        const auto url = job->requestUrl();
        const QString id = url.path().mid(1);
        QByteArray body, mime;
        if (url.host() == "asset") {
            QFile file(app.collection().assetPath(id));
            if (!file.open(QIODevice::ReadOnly)) { job->fail(QWebEngineUrlRequestJob::UrlNotFound); return; }
            body = file.readAll();
            mime = QMimeDatabase().mimeTypeForData(body).name().toUtf8();
            if (!mime.startsWith("image/")) { job->fail(QWebEngineUrlRequestJob::RequestDenied); return; }
        } else if (url.host() == "clip") {
            const auto response = app.dispatch({{"op", "clip.get"}, {"id", id}});
            if (!response.value("ok").toBool()) { job->fail(QWebEngineUrlRequestJob::UrlNotFound); return; }
            const auto clip = response.value("result").toObject();
            QString content;
            QSet<QString> ids;
            for (const auto &value : clip.value("assets").toArray()) ids.insert(value.toObject().value("id").toString());
            if (clip.value("kind").toString() == "article") content = clip.value("html").toString();
            else {
                QTextDocument doc;
                doc.setMarkdown(clip.value("text").toString(), QTextDocument::MarkdownFeatures(QTextDocument::MarkdownDialectGitHub) | QTextDocument::MarkdownNoHTML);
                content = sanitizeMarkdownHtml(doc.toHtml(), ids);
                for (const auto &asset : clip.value("assets").toArray())
                    content += "<p><img src=\"clipper://asset/" + asset.toObject().value("id").toString().toHtmlEscaped() + "\"></p>";
            }
            const auto theme = currentTheme();
            content = decorateReaderCodeBlocks(content);
            const QString style = QString(R"CSS(
:root{--paper:%1;--ink:%2;--surface:%3;--accent:%4;--line:%5;--code:#171a1c;--code-ink:#dce7e7;--code-muted:#78848a}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.75 system-ui,-apple-system,"Noto Sans",sans-serif;overflow-wrap:anywhere}
main{width:100%;max-width:720px;margin:0 auto;padding:48px 42px 84px}
h1,h2,h3,h4,h5,h6{font-family:"Noto Serif",Georgia,serif;line-height:1.22;letter-spacing:-.015em;margin:1.9em 0 .65em;color:var(--ink)}
h1{font-size:2.35rem;margin-top:.25em}h2{font-size:1.7rem}h3{font-size:1.35rem}
p,ul,ol,blockquote,table,figure,.code-card{margin:1.15em 0}
ul,ol{padding-left:1.55em}li+li{margin-top:.32em}
a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:.18em}
img{display:block;max-width:100%;height:auto;margin:1.6em auto;border-radius:8px}
figure{margin-left:0;margin-right:0}figcaption{margin-top:.55em;color:color-mix(in srgb,var(--ink) 65%,transparent);font-size:.86rem;text-align:center}
blockquote{margin-left:0;padding:.25em 0 .25em 1.2em;border-left:3px solid var(--accent);color:color-mix(in srgb,var(--ink) 78%,transparent)}
table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse}th,td{padding:.65em .8em;border:1px solid var(--line);text-align:left}th{background:var(--surface)}
:not(pre)>code{padding:.12em .34em;border:1px solid var(--line);border-radius:4px;background:var(--surface);font:88%/1.4 ui-monospace,"Cascadia Code","Noto Sans Mono",monospace}
article[data-clipper-post]{margin:1.6em 0;padding:1.2em 1.35em;border:1px solid var(--line);border-radius:9px;background:color-mix(in srgb,var(--surface) 56%,transparent)}
article[data-clipper-role="original"]{border-left:3px solid var(--accent)}
article[data-clipper-post]>header{display:flex;gap:.65em;align-items:baseline;margin-bottom:.9em;color:color-mix(in srgb,var(--ink) 66%,transparent);font-size:.86rem}
aside[data-clipper-scope-notice]{margin:1.2em 0;padding:.75em 1em;border:1px solid var(--line);border-radius:7px;background:var(--surface);font-size:.88rem}
.code-card{overflow:hidden;border:1px solid #30383d;border-radius:9px;background:var(--code);color:var(--code-ink);overflow-wrap:normal}
.code-header{display:flex;min-height:40px;align-items:center;justify-content:space-between;gap:12px;padding:6px 8px 6px 13px;border-bottom:1px solid #30383d;color:#aab5b9;font:12px/1.4 system-ui,sans-serif}
.code-language{font-weight:600;letter-spacing:.02em}.code-actions{display:flex;gap:3px}
.code-actions button{min-height:29px;padding:4px 9px;border:0;border-radius:5px;background:transparent;color:#b7c1c5;font:12px/1 system-ui,sans-serif;cursor:pointer}
.code-actions button:hover,.code-actions button:focus-visible{outline:none;background:#293035;color:white}.code-actions button:focus-visible{box-shadow:0 0 0 2px var(--accent)}
.code-original{position:fixed;left:-10000px;top:0;width:1px;height:1px;opacity:0}
.code-scroll{max-width:100%;overflow-x:auto;overscroll-behavior-x:contain}
.code-source{min-width:max-content;margin:0;padding:14px 0;background:transparent;color:var(--code-ink);font:13px/1.72 ui-monospace,"Cascadia Code","Noto Sans Mono",monospace;white-space:normal;overflow:visible}
.code-line{display:grid;grid-template-columns:44px max-content;min-width:100%}
.code-line-number{position:sticky;left:0;z-index:1;padding-right:10px;background:var(--code);color:#58666d;text-align:right;user-select:none}
.code-text{padding:0 18px 0 12px;white-space:pre}
.code-card.wrapped .code-source{min-width:100%}.code-card.wrapped .code-line{grid-template-columns:44px minmax(0,1fr)}.code-card.wrapped .code-text{white-space:pre-wrap;overflow-wrap:anywhere}
.keyword,.type,.preproc{color:#8fd6c8}.string{color:#d9c88f}.comment{color:#7f9297;font-style:italic}.number{color:#dda7c7}.function{color:#9ec7ed}.variable,.symbol{color:#dce7e7}
@media(max-width:560px){body{font-size:16px}main{padding:28px 20px 60px}h1{font-size:1.9rem}.code-line{grid-template-columns:39px max-content}.code-card.wrapped .code-line{grid-template-columns:39px minmax(0,1fr)}.code-text{padding-right:13px;padding-left:9px}.code-line-number{padding-right:8px}}
)CSS").arg(theme.value("background").toString(), theme.value("foreground").toString(),
             theme.value("surface").toString(), theme.value("accent").toString(),
             theme.value("border").toString());
            const QJsonObject controlLabels{
                {QStringLiteral("wrap"), QCoreApplication::translate("ReaderResources", "Wrap")},
                {QStringLiteral("unwrap"), QCoreApplication::translate("ReaderResources", "Unwrap")},
                {QStringLiteral("copied"), QCoreApplication::translate("ReaderResources", "Copied")},
            };
            const QString controls = QStringLiteral(R"JS(
(function(labels){
  function original(card){return card.querySelector('.code-original').value.slice(1);}
  function copied(button){var old=button.textContent;button.textContent=labels.copied;setTimeout(function(){button.textContent=old;},1200);}
  function legacyCopy(card,button){var field=card.querySelector('.code-original');field.focus();field.setSelectionRange(1,field.value.length);try{document.execCommand('copy');copied(button);}catch(_){} }
  document.addEventListener('click',function(event){
    var button=event.target.closest('button');if(!button)return;
    var card=button.closest('.code-card');if(!card)return;
    if(button.classList.contains('code-wrap')){var wrapped=card.classList.toggle('wrapped');button.setAttribute('aria-pressed',wrapped?'true':'false');button.textContent=wrapped?labels.unwrap:labels.wrap;return;}
    if(button.classList.contains('code-copy')){var text=original(card);if(navigator.clipboard&&navigator.clipboard.writeText){try{Promise.resolve(navigator.clipboard.writeText(text)).then(function(){copied(button);},function(){legacyCopy(card,button);});}catch(_){legacyCopy(card,button);}}else legacyCopy(card,button);}
  });
})(%1);
)JS").arg(QString::fromUtf8(QJsonDocument(controlLabels).toJson(QJsonDocument::Compact)));
            const QString document = "<!doctype html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src clipper:; style-src 'unsafe-inline'; script-src 'nonce-clipper-reader-controls'; base-uri 'none'; form-action 'none'\"><style>" + style + "</style></head><body><main>" + content + "</main><script nonce=\"clipper-reader-controls\">" + controls + "</script></body></html>";
            body = document.toUtf8(); mime = "text/html";
        } else { job->fail(QWebEngineUrlRequestJob::UrlNotFound); return; }
        auto *buffer = new QBuffer(job);
        buffer->setData(body); buffer->open(QIODevice::ReadOnly);
        job->reply(mime, buffer);
    }
private:
    Application &app;
};
}

Application::Application(QString root, QString clipId, QObject *parent)
    : QObject(parent), m_root(std::move(root)), m_clipId(std::move(clipId)), m_language(this), m_collection(m_root)
{
    connect(&m_language, &LanguageSettings::languageChanged,
            this, &Application::languageChanged);
}

QVariantMap Application::request(const QVariantMap &value) { return dispatch(QJsonObject::fromVariantMap(value)).toVariantMap(); }
QString Application::readerUrl(const QString &id) const {
    return "clipper://clip/" + QString::fromUtf8(QUrl::toPercentEncoding(id)) + "?t=" + QString::number(QDateTime::currentMSecsSinceEpoch());
}

QJsonObject Application::dispatch(const QJsonObject &value, bool native) {
    const QString op = value.value("op").toString();
    static const QSet<QString> nativeOps{"capture.check", "capture.begin", "capture.finish", "capture.abort", "capture.pending", "capture.mixed", "capture.region", "asset.import", "clip.open", "clip.restore", "settings.get"};
    if (native && !nativeOps.contains(op)) return failure("Operation is not available to browser clients");
    try {
        if (op == "settings.get")
            return success(QJsonObject{{"language", language()},
                                       {"resolvedLanguage", resolvedLanguage()}});
        if (op == "theme.get") return success(currentTheme());
        if (op == "link.open") {
            const auto url = value.value("url").toString();
            if (!webUrl(url)) return failure("Only web links can be opened");
            return QDesktopServices::openUrl(QUrl(url)) ? success(true) : failure("Could not open the source browser");
        }
        if (op == "clip.open") {
            auto found = m_collection.dispatch({{"op", "clip.get"}, {"id", value.value("id")}});
            if (!found.value("ok").toBool()) return found;
            QProcess reader;
            const bool mixed = found.value("result").toObject().value("kind").toString() == "mixed";
            reader.setProgram(mixed
                ? QCoreApplication::applicationDirPath() + "/clipper-native"
                : QCoreApplication::applicationFilePath());
            QStringList arguments{"--data-dir", m_root, "--open", value.value("id").toString()};
            if (!mixed) arguments << "--web-reader";
            reader.setArguments(arguments);
            reader.setStandardOutputFile(QProcess::nullDevice());
            reader.setStandardErrorFile(QProcess::nullDevice());
            const bool launched = reader.startDetached();
            return launched ? success(true) : failure("Could not open the detail window");
        }
        if (op == "file.import" || op == "clipboard.import" || op == "capture.region") {
            QByteArray bytes;
            QString name = "Image";
            if (op == "file.import") {
                QString path = value.value("path").toString();
                if (path.startsWith("file:")) path = QUrl(path).toLocalFile();
                QFile file(path);
                if (!file.open(QIODevice::ReadOnly) || file.size() > 12 * 1024 * 1024) return failure("Image could not be read or exceeds 12 MiB");
                bytes = file.readAll(); name = QFileInfo(path).fileName();
            } else if (op == "capture.region") {
                if (native && !webUrl(value.value("sourceUrl").toString())) return failure("A source URL is required");
                QTemporaryDir captureDirectory;
                if (!captureDirectory.isValid()) return failure("Could not create a screenshot directory");
                auto environment = QProcessEnvironment::systemEnvironment();
                environment.insert("OMASNAP_SCREENSHOT_DIR", captureDirectory.path());
                runProcess("omarchy", {"capture", "screenshot", "region", "save"}, {}, 180000, environment);
                // Omasnap also exits successfully on cancellation or overlay dismissal.
                const auto files = QDir(captureDirectory.path()).entryList(QDir::Files);
                if (files.size() != 1) return failure("Screenshot was cancelled or produced no image");
                QFile file(captureDirectory.filePath(files.first()));
                if (!file.open(QIODevice::ReadOnly) || file.size() > 12 * 1024 * 1024)
                    return failure("Screenshot could not be read or exceeds 12 MiB");
                bytes = file.readAll();
            } else {
                QString clipboardType = "image/png";
                if (op == "clipboard.import") {
                    const auto types = QString::fromUtf8(runProcess("wl-paste", {"--list-types"})).split('\n');
                    clipboardType.clear();
                    for (const auto &candidate : {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/avif"}) {
                        if (types.contains(QString::fromLatin1(candidate))) { clipboardType = candidate; break; }
                    }
                    if (clipboardType.isEmpty()) return failure("Clipboard does not contain a supported image");
                }
                bytes = runProcess("wl-paste", {"--no-newline", "--type", clipboardType});
            }
            const auto mime = QMimeDatabase().mimeTypeForData(bytes).name();
            auto imported = m_collection.dispatch({{"op", "asset.import"}, {"data", QString::fromLatin1(bytes.toBase64())}, {"mime", mime}, {"name", name}});
            if (!imported.value("ok").toBool() || !native || op != "capture.region") return imported;
            return m_collection.dispatch({{"op", "capture.mixed"}, {"text", ""}, {"assetIds", QJsonArray{imported.value("result").toObject().value("id")}}, {"sourceUrl", value.value("sourceUrl")}, {"title", value.value("title")}, {"scope", "region"}});
        }
        if (op == "clipboard.copy") {
            auto found = m_collection.dispatch({{"op", "clip.get"}, {"id", value.value("id")}});
            if (!found.value("ok").toBool()) return found;
            const auto clip = found.value("result").toObject();
            const QString assetId = value.value("assetId").toString();
            if (assetId.isEmpty()) runProcess("wl-copy", {"--type", "text/plain;charset=utf-8"}, clip.value("text").toString().toUtf8());
            else {
                bool owned = false;
                for (const auto &asset : clip.value("assets").toArray()) if (asset.toObject().value("id").toString() == assetId) owned = true;
                if (!owned) return failure("Image does not belong to this clip");
                QFile file(m_collection.assetPath(assetId));
                if (!file.open(QIODevice::ReadOnly)) return failure("Image is missing");
                const auto bytes = file.readAll();
                runProcess("wl-copy", {"--type", QMimeDatabase().mimeTypeForData(bytes).name()}, bytes);
            }
            return success(true);
        }
        return m_collection.dispatch(value);
    } catch (const QString &error) { return failure(error); }
    catch (const std::exception &) { return failure("The operation could not be completed"); }
}

void registerReaderScheme() {
    QWebEngineUrlScheme scheme("clipper");
    scheme.setSyntax(QWebEngineUrlScheme::Syntax::Host);
    scheme.setFlags(QWebEngineUrlScheme::SecureScheme | QWebEngineUrlScheme::LocalScheme | QWebEngineUrlScheme::LocalAccessAllowed);
    QWebEngineUrlScheme::registerScheme(scheme);
}

void configureReader(Application &application) {
    auto *profile = QQuickWebEngineProfile::defaultProfile();
    profile->installUrlSchemeHandler("clipper", new ReaderResources(application));
    profile->setUrlRequestInterceptor(new LocalRequests(&application));
    profile->setPersistentCookiesPolicy(QQuickWebEngineProfile::NoPersistentCookies);
    profile->setHttpCacheType(QQuickWebEngineProfile::MemoryHttpCache);
}
