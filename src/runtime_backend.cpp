#include "runtime_backend.h"

#include <QElapsedTimer>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QProcess>

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {

constexpr qsizetype MaxResponseBytes = 24 * 1024 * 1024;

QJsonObject failure(const QString &message)
{
    return {{"ok", false}, {"error", message}};
}

QJsonObject externalRequest(const QString &program, const QString &helper, const QString &root,
                            const QJsonObject &request)
{
    QProcess process;
    process.start(program, {"--request", "--data-dir", root, "--helper-bin", helper});
    if (!process.waitForStarted(3000))
        return failure("Could not start the selected backend");
    process.write(QJsonDocument(request).toJson(QJsonDocument::Compact));
    process.closeWriteChannel();

    QByteArray output;
    QElapsedTimer timer;
    timer.start();
    while (true) {
        process.waitForReadyRead(50);
        output += process.read(MaxResponseBytes + 1 - output.size());
        process.readAllStandardError();
        if (output.size() > MaxResponseBytes || timer.elapsed() > 200000) {
            process.kill();
            process.waitForFinished(3000);
            return failure("Backend response exceeded its resource limit");
        }
        if (process.state() == QProcess::NotRunning && !process.bytesAvailable())
            break;
    }
    if (process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0)
        return failure("The selected backend failed");
    const QJsonDocument document = QJsonDocument::fromJson(output);
    if (!document.isObject() || !document.object().value("ok").isBool())
        return failure("Backend returned an invalid response");
    return document.object();
}

} // namespace

InstallationLifetimeLock::InstallationLifetimeLock()
{
    const QFileInfo executable(QCoreApplication::applicationFilePath());
    const QDir directory = executable.dir();
    QString prefix;
    if (directory.dirName() == QStringLiteral("bin")
        && executable.fileName() == QStringLiteral("clipper-native")) {
        prefix = directory.absoluteFilePath(QStringLiteral(".."));
    } else if (executable.fileName() == QStringLiteral("clipper-reader")
               && directory.dirName() == QStringLiteral("omarchy-clipper")) {
        const QDir libexec(directory.absoluteFilePath(QStringLiteral("..")));
        if (libexec.dirName() == QStringLiteral("libexec"))
            prefix = libexec.absoluteFilePath(QStringLiteral(".."));
    }
    if (prefix.isEmpty())
        return;

    const QString path = QDir(prefix).absoluteFilePath(
        QStringLiteral("share/omarchy-clipper/backend.lock"));
    m_descriptor = ::open(QFile::encodeName(path).constData(), O_RDONLY | O_NOFOLLOW);
    if (m_descriptor < 0)
        throw std::runtime_error(
            QStringLiteral("Could not open the installation lifetime lock: %1")
                .arg(QString::fromLocal8Bit(std::strerror(errno))).toStdString());
    struct stat status {};
    if (::fstat(m_descriptor, &status) != 0 || !S_ISREG(status.st_mode)
        || status.st_uid != ::geteuid() || status.st_nlink != 1
        || (status.st_mode & 0777) != 0600) {
        ::close(m_descriptor);
        m_descriptor = -1;
        throw std::runtime_error("The installation lifetime lock is unsafe");
    }
    // Do not set close-on-exec: Reader routing replaces this process with the
    // article or mixed detail executable, which must retain the shared lock.
    if (::flock(m_descriptor, LOCK_SH | LOCK_NB) != 0) {
        ::close(m_descriptor);
        m_descriptor = -1;
        throw std::runtime_error(
            "The installation is being changed; retry after it completes");
    }
}

InstallationLifetimeLock::~InstallationLifetimeLock()
{
    if (m_descriptor >= 0)
        ::close(m_descriptor);
}

RuntimeBackend::RuntimeBackend(QString root, QString backend)
    : m_root(std::move(root))
    , m_backend(std::move(backend))
{
    const QFileInfo executable(QCoreApplication::applicationFilePath());
    const QDir directory = executable.dir();
    if (directory.dirName() == QStringLiteral("bin")
        && executable.fileName() == QStringLiteral("clipper-native")) {
        m_helper = directory.absoluteFilePath(
            QStringLiteral("../libexec/omarchy-clipper/clipper-helper"));
    } else {
        m_helper = directory.absoluteFilePath(QStringLiteral("clipper-helper"));
    }
    if (!m_backend.isEmpty())
        return;
    if (directory.dirName() == QStringLiteral("bin")
        && executable.fileName() == QStringLiteral("clipper-native")) {
        m_backend = directory.absoluteFilePath(QStringLiteral("clipper"));
    } else if (executable.fileName() == QStringLiteral("clipper-reader")
               && directory.dirName() == QStringLiteral("omarchy-clipper")) {
        m_backend = directory.absoluteFilePath(QStringLiteral("../../bin/clipper"));
    } else {
        m_backend = directory.absoluteFilePath(
            QStringLiteral("../backend/target/release/omarchy-clipper-backend"));
    }
}

QJsonObject RuntimeBackend::dispatch(const QJsonObject &request)
{
    return externalRequest(m_backend, m_helper, m_root, request);
}

QByteArray RuntimeBackend::readAsset(const QString &id, QString *mime, QString *error) const
{
    const QJsonObject response = externalRequest(
        m_backend, m_helper, m_root, {{"op", "asset.read"}, {"id", id}});
    if (!response.value("ok").toBool()) {
        if (error)
            *error = response.value("error").toString(QStringLiteral("Image is missing"));
        return {};
    }
    const QJsonObject asset = response.value("result").toObject();
    const QString assetMime = asset.value("mime").toString();
    const QByteArray encoded = asset.value("data").toString().toLatin1();
    const QByteArray bytes = QByteArray::fromBase64(encoded, QByteArray::AbortOnBase64DecodingErrors);
    if (bytes.isEmpty() || bytes.size() > 12 * 1024 * 1024
        || !assetMime.startsWith(QStringLiteral("image/"))) {
        if (error)
            *error = QStringLiteral("Image is missing");
        return {};
    }
    if (mime)
        *mime = assetMime;
    return bytes;
}
