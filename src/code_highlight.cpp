#include "code_highlight.h"

#include <QCache>
#include <QCryptographicHash>
#include <QHash>
#include <QProcess>
#include <QStandardPaths>
#include <QSet>
#include <QXmlStreamReader>

namespace clipper {
namespace {

struct Language {
    const char *sourceHighlightName;
    const char *displayName;
};

const QHash<QString, Language> &languages()
{
    static const QHash<QString, Language> values{
        {QStringLiteral("bash"), {"bash", "Bash"}},
        {QStringLiteral("sh"), {"sh", "Shell"}},
        {QStringLiteral("shell"), {"sh", "Shell"}},
        {QStringLiteral("c"), {"c", "C"}},
        {QStringLiteral("cpp"), {"cpp", "C++"}},
        {QStringLiteral("c++"), {"cpp", "C++"}},
        {QStringLiteral("cxx"), {"cpp", "C++"}},
        {QStringLiteral("cs"), {"csharp", "C#"}},
        {QStringLiteral("csharp"), {"csharp", "C#"}},
        {QStringLiteral("css"), {"css", "CSS"}},
        {QStringLiteral("go"), {"go", "Go"}},
        {QStringLiteral("html"), {"html", "HTML"}},
        {QStringLiteral("java"), {"java", "Java"}},
        {QStringLiteral("javascript"), {"javascript", "JavaScript"}},
        {QStringLiteral("js"), {"javascript", "JavaScript"}},
        {QStringLiteral("json"), {"json", "JSON"}},
        {QStringLiteral("lua"), {"lua", "Lua"}},
        {QStringLiteral("make"), {"makefile", "Makefile"}},
        {QStringLiteral("makefile"), {"makefile", "Makefile"}},
        {QStringLiteral("perl"), {"perl", "Perl"}},
        {QStringLiteral("php"), {"php", "PHP"}},
        {QStringLiteral("python"), {"python", "Python"}},
        {QStringLiteral("py"), {"python", "Python"}},
        {QStringLiteral("ruby"), {"ruby", "Ruby"}},
        {QStringLiteral("rb"), {"ruby", "Ruby"}},
        {QStringLiteral("rust"), {"rs", "Rust"}},
        {QStringLiteral("rs"), {"rs", "Rust"}},
        {QStringLiteral("sql"), {"sql", "SQL"}},
        {QStringLiteral("xml"), {"xml", "XML"}},
        {QStringLiteral("typescript"), {nullptr, "TypeScript"}},
        {QStringLiteral("ts"), {nullptr, "TypeScript"}},
        {QStringLiteral("yaml"), {nullptr, "YAML"}},
        {QStringLiteral("yml"), {nullptr, "YAML"}},
    };
    return values;
}

QString cacheKey(const QString &source, const QString &language)
{
    QCryptographicHash hash(QCryptographicHash::Sha256);
    hash.addData(language.toUtf8());
    hash.addData(QByteArrayLiteral("\0"));
    hash.addData(source.toUtf8());
    return QString::fromLatin1(hash.result().toHex());
}

QString highlightedFragment(const QByteArray &output, const QString &source)
{
    const QString html = QString::fromUtf8(output);
    const QString startTag = QStringLiteral("<pre><tt>");
    const QString endTag = QStringLiteral("</tt></pre>");
    const qsizetype start = html.indexOf(startTag);
    const qsizetype end = html.lastIndexOf(endTag);
    if (start < 0 || end < start)
        return {};
    const QString fragment = html.mid(start + startTag.size(), end - start - startTag.size());
    static const QSet<QString> classes{
        "normal", "keyword", "type", "preproc", "string", "comment", "number",
        "symbol", "function", "variable", "cbracket", "classname", "usertype",
        "predef_var", "predef_func", "specialchar", "regexp", "label", "todo",
        "url", "date", "time", "file", "ip", "name", "argument", "value",
    };
    QXmlStreamReader reader("<root>" + fragment + "</root>");
    QString safeHtml;
    QString decoded;
    int depth = 0;
    while (!reader.atEnd()) {
        reader.readNext();
        if (reader.isStartElement()) {
            if (!reader.namespaceUri().isEmpty() || !reader.namespaceDeclarations().isEmpty())
                return {};
            if (depth == 0) {
                if (reader.name() != "root" || !reader.attributes().isEmpty())
                    return {};
            } else {
                if (depth > 64 || reader.name() != "span" || reader.attributes().size() != 1)
                    return {};
                const auto attribute = reader.attributes().first();
                if (attribute.qualifiedName() != "class" || !classes.contains(attribute.value().toString()))
                    return {};
                safeHtml += "<span class=\"" + attribute.value().toString() + "\">";
            }
            ++depth;
        } else if (reader.isEndElement()) {
            if (--depth > 0)
                safeHtml += "</span>";
        } else if (reader.isCharacters()) {
            const QString text = reader.text().toString();
            decoded += text;
            safeHtml += text.toHtmlEscaped();
        } else if (reader.isDTD() || reader.isProcessingInstruction()
                   || reader.isEntityReference() || reader.isComment()) {
            return {};
        }
    }
    // Reconstruct only text and allowlisted spans, and reject a highlighter that
    // changes the source. Reader controls are generated separately from this data.
    return !reader.hasError() && decoded == source ? safeHtml : QString();
}

} // namespace

CodeHighlightResult highlightCode(const QString &source, const QString &languageHint,
                                  bool allowExternalHighlighter)
{
    CodeHighlightResult fallback{source.toHtmlEscaped(), QStringLiteral("Plain text"), false, false};
    const QString normalized = languageHint.trimmed().toLower();
    const auto language = languages().constFind(normalized);
    if (language == languages().constEnd())
        return fallback;

    fallback.displayLanguage = QString::fromLatin1(language->displayName);
    fallback.knownLanguage = true;
    if (!allowExternalHighlighter || !language->sourceHighlightName)
        return fallback;

    static QCache<QString, CodeHighlightResult> cache(2048);
    const QString key = cacheKey(source, normalized);
    if (const CodeHighlightResult *cached = cache.object(key))
        return *cached;

    // Bound work in the GUI process. Large blocks remain readable plain text.
    constexpr qsizetype maximumInputBytes = 512 * 1024;
    const QByteArray input = source.toUtf8();
    if (input.size() > maximumInputBytes)
        return fallback;
    const auto cachedFallback = [&key, &fallback, &input] {
        cache.insert(key, new CodeHighlightResult(fallback), qMax(1, input.size() / 1024));
        return fallback;
    };

    static const QString executable =
        QStandardPaths::findExecutable(QStringLiteral("source-highlight"));
    if (executable.isEmpty())
        return fallback;

    QProcess process;
    process.setProcessChannelMode(QProcess::SeparateChannels);
    process.start(executable,
                  {QStringLiteral("--src-lang=%1")
                       .arg(QString::fromLatin1(language->sourceHighlightName)),
                   QStringLiteral("--out-format=html-css"), QStringLiteral("--no-doc")});
    if (!process.waitForStarted(150))
        return cachedFallback();
    process.write(input);
    process.closeWriteChannel();
    if (!process.waitForFinished(200)) {
        process.kill();
        process.waitForFinished(100);
        return cachedFallback();
    }
    const QByteArray output = process.readAllStandardOutput();
    if (process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0
        || output.size() > input.size() * 16 + 64 * 1024) {
        return cachedFallback();
    }

    const QString html = highlightedFragment(output, source);
    if (html.isEmpty() && !source.isEmpty())
        return cachedFallback();
    CodeHighlightResult result{html, fallback.displayLanguage, true, true};
    cache.insert(key, new CodeHighlightResult(result),
                 qMax(1, result.html.toUtf8().size() / 1024));
    return result;
}

} // namespace clipper
