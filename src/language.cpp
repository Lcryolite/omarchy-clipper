#include "language.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocale>

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
    , m_settingsPath(QDir::home().filePath(QStringLiteral(".config/omarchy/shell.json")))
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
}

void LanguageSettings::refresh()
{
    QString next = QStringLiteral("system");
    QFile file(m_settingsPath);
    if (file.open(QIODevice::ReadOnly)) {
        QJsonParseError error;
        const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &error);
        if (error.error == QJsonParseError::NoError && document.isObject())
            next = configuredLanguage(document.object());
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
