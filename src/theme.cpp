#include "theme.h"

#include <QFile>
#include <QHash>
#include <QRegularExpression>
#include <QStandardPaths>

QJsonObject currentTheme()
{
    QJsonObject theme{{QStringLiteral("background"), QStringLiteral("#2d353b")},
                      {QStringLiteral("surface"), QStringLiteral("#343f44")},
                      {QStringLiteral("foreground"), QStringLiteral("#d3c6aa")},
                      {QStringLiteral("muted"), QStringLiteral("#859289")},
                      {QStringLiteral("accent"), QStringLiteral("#7fbbb3")},
                      {QStringLiteral("border"), QStringLiteral("#475258")},
                      {QStringLiteral("fontFamily"), QStringLiteral("monospace")}};
    const QString state = QStandardPaths::writableLocation(QStandardPaths::GenericStateLocation);
    QFile colors(state + QStringLiteral("/omarchy/current/theme/colors.toml"));
    if (!colors.open(QIODevice::ReadOnly))
        return theme;

    const QRegularExpression pattern(
        QStringLiteral("^([a-z_]+)\\s*=\\s*[\"'](#[0-9a-fA-F]{6})[\"']"));
    const QHash<QString, QString> mapping{
        {QStringLiteral("background"), QStringLiteral("background")},
        {QStringLiteral("foreground"), QStringLiteral("foreground")},
        {QStringLiteral("accent"), QStringLiteral("accent")},
        {QStringLiteral("light_foreground"), QStringLiteral("muted")},
        {QStringLiteral("lighter_background"), QStringLiteral("surface")},
        {QStringLiteral("muted"), QStringLiteral("border")},
    };
    for (const QString &line : QString::fromUtf8(colors.readAll()).split(QLatin1Char('\n'))) {
        const QRegularExpressionMatch match = pattern.match(line.trimmed());
        if (match.hasMatch() && mapping.contains(match.captured(1)))
            theme.insert(mapping.value(match.captured(1)), match.captured(2));
    }
    return theme;
}
