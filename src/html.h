#pragma once

#include <QSet>
#include <QString>

QString sanitizeCapturedHtml(const QString &html, const QSet<QString> &ownedAssetIds);
QString sanitizeMarkdownHtml(const QString &html, const QSet<QString> &ownedAssetIds);
QString decorateReaderCodeBlocks(const QString &html);
