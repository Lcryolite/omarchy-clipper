#pragma once

#include <QByteArray>
#include <QString>

struct InspectedImage
{
    QByteArray bytes;
    QString mime;
    QString extension;
    int width;
    int height;
};

InspectedImage inspectImage(const QString &base64Data, const QString &declaredMime);
