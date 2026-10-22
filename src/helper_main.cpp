#include "desktop.h"
#include "html.h"
#include "image.h"
#include "validation_error.h"

#include <QCoreApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QSet>

#include <cstdio>
#include <stdexcept>

#ifndef CLIPPER_VERSION
#define CLIPPER_VERSION "0.1.0"
#endif

namespace {

constexpr qsizetype MaxRequestBytes = 24 * 1024 * 1024;

QJsonObject success(const QJsonValue &result)
{
    return {{"ok", true}, {"result", result}};
}

QJsonObject failure(const QString &message)
{
    return {{"ok", false}, {"error", message}};
}

QJsonObject failure(const ValidationError &error)
{
    QJsonObject result{{"ok", false}, {"error", QString::fromUtf8(error.what())}};
    if (!error.code.isEmpty())
        result.insert("errorCode", error.code);
    return result;
}

QSet<QString> assetIds(const QJsonValue &value)
{
    if (!value.isArray())
        throw std::runtime_error("assetIds must be an array");
    QSet<QString> result;
    for (const QJsonValue &entry : value.toArray()) {
        if (!entry.isString() || entry.toString().isEmpty())
            throw std::runtime_error("assetIds contains an invalid ID");
        result.insert(entry.toString());
    }
    return result;
}

QJsonObject dispatch(const QJsonObject &request)
{
    try {
        const QString op = request.value("op").toString();
        if (op.isEmpty())
            throw std::runtime_error("Missing operation");
        if (op == "html.sanitize_capture") {
            if (!request.value("html").isString())
                throw std::runtime_error("html must be a string");
            const QString html = sanitizeCapturedHtml(
                request.value("html").toString(), assetIds(request.value("assetIds")));
            return success(QJsonObject{{"html", html}});
        }
        if (op == "image.inspect") {
            if (!request.value("data").isString())
                throw std::runtime_error("data must be base64 text");
            if (!request.value("mime").isString())
                throw std::runtime_error("mime is required");
            const InspectedImage image = inspectImage(
                request.value("data").toString(), request.value("mime").toString());
            return success(QJsonObject{{"mime", image.mime},
                                       {"extension", image.extension},
                                       {"width", image.width},
                                       {"height", image.height}});
        }
        if (op == "desktop.theme_get")
            return success(desktopTheme());
        if (op == "desktop.link_open") {
            if (!request.value("url").isString())
                throw std::runtime_error("url must be a string");
            return success(openDesktopLink(request.value("url").toString()));
        }
        if (op == "desktop.file_image_read") {
            if (!request.value("path").isString())
                throw std::runtime_error("path must be a string");
            return success(readDesktopFileImage(request.value("path").toString()));
        }
        if (op == "desktop.clipboard_image_read")
            return success(readDesktopClipboardImage());
        if (op == "desktop.capture_region") {
            if (!request.value("sourceUrl").isString())
                throw std::runtime_error("sourceUrl must be a string");
            if (!request.value("requireSourceUrl").isBool())
                throw std::runtime_error("requireSourceUrl must be a boolean");
            return success(captureDesktopRegion(request.value("sourceUrl").toString(),
                                                request.value("requireSourceUrl").toBool()));
        }
        if (op == "desktop.clipboard_text_write") {
            if (!request.value("text").isString())
                throw std::runtime_error("text must be a string");
            writeDesktopClipboardText(request.value("text").toString());
            return success(true);
        }
        if (op == "desktop.clipboard_image_write") {
            if (!request.value("data").isString())
                throw std::runtime_error("data must be base64 text");
            if (!request.value("mime").isString())
                throw std::runtime_error("mime is required");
            writeDesktopClipboardImage(request.value("data").toString(),
                                       request.value("mime").toString());
            return success(true);
        }
        throw std::runtime_error(("Unknown operation: " + op).toStdString());
    } catch (const ValidationError &error) {
        return failure(error);
    } catch (const std::exception &error) {
        return failure(QString::fromUtf8(error.what()));
    }
}

void writeResponse(const QJsonObject &response)
{
    const QByteArray output = QJsonDocument(response).toJson(QJsonDocument::Compact) + '\n';
    std::fwrite(output.constData(), 1, size_t(output.size()), stdout);
}

QByteArray readRequest(QFile &input)
{
    QByteArray result;
    result.reserve(64 * 1024);
    while (result.size() <= MaxRequestBytes) {
        const qsizetype remaining = MaxRequestBytes + 1 - result.size();
        const QByteArray chunk = input.read(qMin<qsizetype>(64 * 1024, remaining));
        if (chunk.isEmpty()) {
            if (input.error() != QFileDevice::NoError)
                throw std::runtime_error("Could not read request");
            break;
        }
        result.append(chunk);
    }
    return result;
}

} // namespace

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    const QStringList arguments = application.arguments();
    if (arguments.contains("--version")) {
        std::puts(CLIPPER_VERSION);
        return 0;
    }
    if (!arguments.contains("--request")) {
        std::fputs("Use --request\n", stderr);
        return 2;
    }

    QFile input;
    if (!input.open(stdin, QIODevice::ReadOnly))
        return 2;
    QByteArray bytes;
    try {
        bytes = readRequest(input);
    } catch (const std::exception &error) {
        std::fprintf(stderr, "%s\n", error.what());
        return 2;
    }
    if (bytes.size() > MaxRequestBytes) {
        writeResponse(failure("Request is too large"));
        return 0;
    }
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(bytes, &error);
    writeResponse(dispatch(error.error == QJsonParseError::NoError && document.isObject()
                               ? document.object()
                               : QJsonObject{}));
    return 0;
}
