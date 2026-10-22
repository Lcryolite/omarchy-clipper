#include "image.h"

#include "validation_error.h"

#include <QBuffer>
#include <QImage>
#include <QImageReader>

namespace {

void require(bool condition, const QString &message, const QString &code = {})
{
    if (!condition && !code.isEmpty())
        throw ValidationError(message, code);
    if (!condition)
        throw std::runtime_error(message.toStdString());
}

} // namespace

InspectedImage inspectImage(const QString &base64Data, const QString &declaredMime)
{
    constexpr int decodedRasterBudgetMiB = 64;
    constexpr quint64 decodedRasterBudget = quint64(decodedRasterBudgetMiB) * 1024 * 1024;
    const QByteArray encoded = base64Data.toLatin1();
    require(encoded.size() <= 16 * 1024 * 1024 + 16, "Asset exceeds the 12 MiB limit");
    const QByteArray bytes = QByteArray::fromBase64(
        encoded, QByteArray::AbortOnBase64DecodingErrors);
    require(!bytes.isNull() && !bytes.isEmpty(), "Asset data is not valid base64 image data");
    require(bytes.size() <= 12 * 1024 * 1024, "Asset exceeds the 12 MiB limit");

    QBuffer buffer;
    buffer.setData(bytes);
    require(buffer.open(QIODevice::ReadOnly), "Could not inspect asset data");
    QImageReader reader(&buffer);
    reader.setDecideFormatFromContent(true);
    const QByteArray format = reader.format().toLower();
    require(reader.canRead(), "Asset is not a valid image", "IMAGE_DECODE_FAILED");
    const QSize encodedSize = reader.size();
    const QImage::Format decodedFormat = reader.imageFormat();
    require(encodedSize.width() > 0 && encodedSize.height() > 0,
            "Asset image dimensions are unsupported", "IMAGE_DIMENSIONS_UNSUPPORTED");
    const quint64 width = quint64(encodedSize.width());
    const quint64 height = quint64(encodedSize.height());
    quint64 bitsPerPixel = 32;
    if (decodedFormat != QImage::Format_Invalid) {
        bitsPerPixel = qMax<quint64>(bitsPerPixel, QImage::toPixelFormat(decodedFormat).bitsPerPixel());
    } else {
        require(format == "png" || format == "jpeg" || format == "jpg"
                    || format == "gif" || format == "webp" || format == "bmp",
                "Asset image format is unsupported", "IMAGE_FORMAT_UNSUPPORTED");
        if (format == "png")
            bitsPerPixel = 64;
    }
    const quint64 bytesPerPixel = (bitsPerPixel + 7) / 8;
    require(bytesPerPixel > 0 && width <= decodedRasterBudget / bytesPerPixel
                && height <= decodedRasterBudget / (width * bytesPerPixel),
            "Asset image dimensions are unsupported", "IMAGE_DIMENSIONS_UNSUPPORTED");
    const int allocationLimit = QImageReader::allocationLimit();
    if (allocationLimit <= 0 || allocationLimit > decodedRasterBudgetMiB)
        QImageReader::setAllocationLimit(decodedRasterBudgetMiB);
    const QImage image = reader.read();
    require(!image.isNull(), "Asset is not a valid image", "IMAGE_DECODE_FAILED");
    require(image.width() > 0 && image.height() > 0
                && quint64(image.sizeInBytes()) <= decodedRasterBudget,
            "Asset image dimensions are unsupported", "IMAGE_DIMENSIONS_UNSUPPORTED");

    QString mime;
    QString extension;
    if (format == "png") { mime = "image/png"; extension = "png"; }
    else if (format == "jpeg" || format == "jpg") { mime = "image/jpeg"; extension = "jpg"; }
    else if (format == "gif") { mime = "image/gif"; extension = "gif"; }
    else if (format == "webp") { mime = "image/webp"; extension = "webp"; }
    else if (format == "bmp") { mime = "image/bmp"; extension = "bmp"; }
    else throw ValidationError("Asset image format is unsupported", "IMAGE_FORMAT_UNSUPPORTED");

    QString declared = declaredMime.trimmed().toLower();
    if (declared == "image/jpg")
        declared = "image/jpeg";
    require(declared == mime, "Declared MIME type does not match image data",
            "IMAGE_MIME_MISMATCH");
    return {bytes, mime, extension, image.width(), image.height()};
}
