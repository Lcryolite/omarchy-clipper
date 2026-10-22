#pragma once

#include <QJsonObject>
#include <QString>

QJsonObject desktopTheme();
bool openDesktopLink(const QString &url);
QJsonObject readDesktopFileImage(const QString &path);
QJsonObject readDesktopClipboardImage();
QJsonObject captureDesktopRegion(const QString &sourceUrl, bool requireSourceUrl);
void writeDesktopClipboardText(const QString &text);
void writeDesktopClipboardImage(const QString &data, const QString &mime);
