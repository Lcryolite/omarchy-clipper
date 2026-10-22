#include "desktop.h"

#include "image.h"
#include "theme.h"

#include <QElapsedTimer>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonValue>
#include <QMimeDatabase>
#include <QProcess>
#include <QProcessEnvironment>
#include <QTemporaryDir>
#include <QUrl>

#include <csignal>
#include <stdexcept>
#include <unistd.h>

namespace {

constexpr qsizetype MaxImageBytes = 12 * 1024 * 1024;
constexpr qsizetype MaxDiagnosticBytes = 8 * 1024;
constexpr int DesktopProcessTimeoutMs = 2000;

[[noreturn]] void fail(const QString &message)
{
    throw std::runtime_error(message.toStdString());
}

void killProcessGroup(qint64 pid)
{
    if (pid > 0)
        ::kill(-pid_t(pid), SIGKILL);
}

void stopProcess(QProcess &process, qint64 pid)
{
    killProcessGroup(pid);
    process.kill();
    process.waitForFinished(3000);
}

QByteArray runDesktopProcess(const QString &program, const QStringList &arguments,
                             const QByteArray &input = {},
                             qsizetype outputLimit = MaxImageBytes,
                             int timeoutMs = DesktopProcessTimeoutMs,
                             QProcessEnvironment environment = QProcessEnvironment::systemEnvironment())
{
    QProcess process;
    process.setProcessEnvironment(environment);
    process.setChildProcessModifier([] { ::setpgid(0, 0); });
    process.start(program, arguments);
    if (!process.waitForStarted(3000))
        fail(QString("Could not start %1").arg(program));
    const qint64 processGroup = process.processId();
    if (!input.isEmpty())
        process.write(input);
    process.closeWriteChannel();

    QByteArray output;
    QByteArray diagnostics;
    QElapsedTimer timer;
    timer.start();
    while (process.state() != QProcess::NotRunning) {
        process.waitForFinished(25);
        output += process.readAllStandardOutput();
        diagnostics += process.readAllStandardError();
        if (output.size() > outputLimit || diagnostics.size() > MaxDiagnosticBytes) {
            stopProcess(process, processGroup);
            fail(QString("%1 output is too large").arg(program));
        }
        if (timer.elapsed() >= timeoutMs) {
            stopProcess(process, processGroup);
            fail(QString("%1 timed out").arg(program));
        }
    }
    output += process.readAllStandardOutput();
    diagnostics += process.readAllStandardError();
    if (output.size() > outputLimit || diagnostics.size() > MaxDiagnosticBytes)
        fail(QString("%1 output is too large").arg(program));
    if (process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0) {
        killProcessGroup(processGroup);
        fail(QString("%1 was cancelled or failed").arg(program));
    }
    return output;
}

bool isWebUrl(const QString &text)
{
    const QUrl url(text);
    return url.isValid() && !url.host().isEmpty() && url.userInfo().isEmpty()
        && (url.scheme() == "https" || url.scheme() == "http");
}

QJsonObject inspectedPayload(const QByteArray &bytes, const QString &name)
{
    const QString sniffedMime = QMimeDatabase().mimeTypeForData(bytes).name();
    const QString data = QString::fromLatin1(bytes.toBase64());
    const InspectedImage image = inspectImage(data, sniffedMime);
    return {{"data", data}, {"mime", image.mime}, {"name", name}};
}

QJsonObject readImageFile(const QString &path, const QString &name, const QString &error)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size() > MaxImageBytes)
        fail(error);
    return inspectedPayload(file.read(MaxImageBytes + 1), name);
}

} // namespace

QJsonObject desktopTheme()
{
    return currentTheme();
}

bool openDesktopLink(const QString &url)
{
    if (!isWebUrl(url))
        fail("Only web links can be opened");
    runDesktopProcess("xdg-open", {url}, {}, 64 * 1024);
    return true;
}

QJsonObject readDesktopFileImage(const QString &value)
{
    QString path = value;
    if (path.startsWith("file:"))
        path = QUrl(path).toLocalFile();
    return readImageFile(path, QFileInfo(path).fileName(),
                         "Image could not be read or exceeds 12 MiB");
}

QJsonObject readDesktopClipboardImage()
{
    const QStringList types = QString::fromUtf8(
        runDesktopProcess("wl-paste", {"--list-types"}, {}, 64 * 1024)).split('\n');
    QString selected;
    for (const char *candidate : {"image/png", "image/jpeg", "image/webp", "image/gif",
                                  "image/bmp", "image/avif"}) {
        if (types.contains(QString::fromLatin1(candidate))) {
            selected = QString::fromLatin1(candidate);
            break;
        }
    }
    if (selected.isEmpty())
        fail("Clipboard does not contain a supported image");
    const QByteArray bytes = runDesktopProcess(
        "wl-paste", {"--no-newline", "--type", selected}, {}, MaxImageBytes);
    return inspectedPayload(bytes, "Image");
}

QJsonObject captureDesktopRegion(const QString &sourceUrl, bool requireSourceUrl)
{
    if (requireSourceUrl && !isWebUrl(sourceUrl))
        fail("A source URL is required");
    QTemporaryDir directory;
    if (!directory.isValid())
        fail("Could not create a screenshot directory");

    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.insert("OMASNAP_SCREENSHOT_DIR", directory.path());
    runDesktopProcess("omarchy", {"capture", "screenshot", "region", "save"}, {},
                      64 * 1024, 180000, environment);

    const QStringList files = QDir(directory.path()).entryList(QDir::Files);
    if (files.size() != 1)
        fail("Screenshot was cancelled or produced no image");
    return readImageFile(directory.filePath(files.first()), "Image",
                         "Screenshot could not be read or exceeds 12 MiB");
}

void writeDesktopClipboardText(const QString &text)
{
    runDesktopProcess("wl-copy", {"--type", "text/plain;charset=utf-8"}, text.toUtf8(),
                      64 * 1024);
}

void writeDesktopClipboardImage(const QString &data, const QString &mime)
{
    const InspectedImage image = inspectImage(data, mime);
    runDesktopProcess("wl-copy", {"--type", image.mime}, image.bytes, 64 * 1024);
}
