#include "html.h"
#include "code_highlight.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QRegularExpression>
#include <QUrl>

#include <libxml/HTMLparser.h>
#include <libxml/HTMLtree.h>
#include <libxml/tree.h>

#include <stdexcept>
#include <vector>

namespace {

const QSet<QString> allowedTags = {
    "a", "article", "aside", "b", "blockquote", "br", "code", "dd", "del", "details",
    "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "i", "img", "li", "mark", "ol", "p", "pre", "s",
    "section", "small", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "time", "tr", "ul"
};

QString xmlString(const xmlChar *value)
{
    return value ? QString::fromUtf8(reinterpret_cast<const char *>(value)) : QString();
}

bool safeHttpUrl(const QString &value)
{
    const QUrl url(value, QUrl::StrictMode);
    return url.isValid() && (url.scheme() == "http" || url.scheme() == "https")
        && !url.host().isEmpty();
}

bool safeSemanticAttribute(const QString &tag, const QString &name, const QString &value)
{
    static const QRegularExpression token("^[A-Za-z0-9_.+-]{1,64}$");
    static const QRegularExpression language("^(?:language|lang)-[A-Za-z0-9_.+#-]{1,32}$");
    static const QRegularExpression dateTime("^[0-9TtZz:+.-]{4,40}$");

    if (tag == "code" && name == "class")
        return language.match(value).hasMatch();
    if (tag == "section" && name == "data-clipper-scope")
        return token.match(value).hasMatch();
    if (tag == "section" && name == "data-clipper-cleaning")
        return value == "default" || value == "deep";
    if (tag == "article" && name == "data-clipper-post")
        return value.isEmpty() || value == "true";
    if (tag == "article" && name == "data-clipper-role")
        return value == "original" || value == "reply";
    if (tag == "strong" && name == "data-clipper-author")
        return value.isEmpty() || value == "true";
    if (tag == "aside" && name == "data-clipper-scope-notice")
        return value.isEmpty() || value == "true";
    if (tag == "time" && name == "datetime")
        return dateTime.match(value).hasMatch();
    return false;
}

xmlNodePtr findElement(xmlNodePtr node, const QString &name)
{
    for (xmlNodePtr current = node; current; current = current->next) {
        if (current->type == XML_ELEMENT_NODE
            && xmlString(current->name).compare(name, Qt::CaseInsensitive) == 0) {
            return current;
        }
        if (xmlNodePtr found = findElement(current->children, name))
            return found;
    }
    return nullptr;
}

void rejectActiveContent(xmlNodePtr node)
{
    for (xmlNodePtr current = node; current; current = current->next) {
        if (current->type == XML_ELEMENT_NODE) {
            const QString tag = xmlString(current->name).toLower();
            if (tag == "script" || tag == "style" || tag == "iframe" || tag == "object"
                || tag == "embed") {
                throw std::runtime_error("Captured HTML contains active content");
            }
            for (xmlAttrPtr attr = current->properties; attr; attr = attr->next) {
                const QString name = xmlString(attr->name).toLower();
                if (name.startsWith("on") || name == "style" || name == "srcset")
                    throw std::runtime_error("Captured HTML contains active content in an unsafe attribute");
                xmlChar *raw = xmlNodeListGetString(current->doc, attr->children, 1);
                const QString value = xmlString(raw).trimmed();
                xmlFree(raw);
                if (name == "href" && !safeHttpUrl(value))
                    throw std::runtime_error("Captured HTML contains an unsafe link");
                if (name == "src" && !value.startsWith("clipper://asset/"))
                    throw std::runtime_error("Captured HTML contains a non-local image");
            }
        }
        rejectActiveContent(current->children);
    }
}

void sanitizeNode(xmlNodePtr node, const QSet<QString> &ownedAssetIds)
{
    for (xmlNodePtr current = node; current;) {
        xmlNodePtr next = current->next;
        if (current->type == XML_COMMENT_NODE) {
            xmlUnlinkNode(current);
            xmlFreeNode(current);
            current = next;
            continue;
        }
        if (current->type == XML_ELEMENT_NODE) {
            const QString tag = xmlString(current->name).toLower();
            if (!allowedTags.contains(tag)) {
                sanitizeNode(current->children, ownedAssetIds);
                xmlNodePtr child = current->children;
                while (child) {
                    xmlNodePtr childNext = child->next;
                    xmlUnlinkNode(child);
                    xmlAddPrevSibling(current, child);
                    child = childNext;
                }
                xmlUnlinkNode(current);
                xmlFreeNode(current);
                current = next;
                continue;
            }

            for (xmlAttrPtr attr = current->properties; attr;) {
                xmlAttrPtr attrNext = attr->next;
                const QString name = xmlString(attr->name).toLower();
                xmlChar *raw = xmlNodeListGetString(current->doc, attr->children, 1);
                const QString value = xmlString(raw).trimmed();
                xmlFree(raw);
                bool keep = name == "title" || name == "alt"
                    || ((tag == "td" || tag == "th") && (name == "colspan" || name == "rowspan"))
                    || (tag == "a" && name == "href") || (tag == "img" && name == "src")
                    || safeSemanticAttribute(tag, name, value);
                if (!keep)
                    xmlRemoveProp(attr);
                attr = attrNext;
            }

            if (tag == "img") {
                xmlChar *raw = xmlGetProp(current, BAD_CAST "src");
                const QString src = xmlString(raw);
                xmlFree(raw);
                const QString prefix = "clipper://asset/";
                const QString id = src.mid(prefix.size());
                static const QRegularExpression assetId("^[A-Za-z0-9_-]{1,128}$");
                if (!src.startsWith(prefix) || !assetId.match(id).hasMatch()
                    || !ownedAssetIds.contains(id)) {
                    throw std::runtime_error("Captured HTML references an unowned asset");
                }
            }
        }
        sanitizeNode(current->children, ownedAssetIds);
        current = next;
    }
}

void sanitizeMarkdownNode(xmlNodePtr node, const QSet<QString> &ownedAssetIds)
{
    for (xmlNodePtr current = node; current;) {
        xmlNodePtr next = current->next;
        if (current->type == XML_COMMENT_NODE) {
            xmlUnlinkNode(current);
            xmlFreeNode(current);
            current = next;
            continue;
        }
        if (current->type == XML_ELEMENT_NODE) {
            const QString tag = xmlString(current->name).toLower();
            if (tag == "script" || tag == "style" || tag == "iframe" || tag == "object"
                || tag == "embed") {
                xmlUnlinkNode(current);
                xmlFreeNode(current);
                current = next;
                continue;
            }

            if (!allowedTags.contains(tag)) {
                sanitizeMarkdownNode(current->children, ownedAssetIds);
                xmlNodePtr child = current->children;
                while (child) {
                    xmlNodePtr childNext = child->next;
                    xmlUnlinkNode(child);
                    xmlAddPrevSibling(current, child);
                    child = childNext;
                }
                xmlUnlinkNode(current);
                xmlFreeNode(current);
                current = next;
                continue;
            }

            bool removeImage = false;
            for (xmlAttrPtr attr = current->properties; attr;) {
                xmlAttrPtr attrNext = attr->next;
                const QString name = xmlString(attr->name).toLower();
                xmlChar *raw = xmlNodeListGetString(current->doc, attr->children, 1);
                const QString value = xmlString(raw).trimmed();
                xmlFree(raw);
                bool keep = name == "title" || name == "alt"
                    || ((tag == "td" || tag == "th") && (name == "colspan" || name == "rowspan"));
                if (tag == "a" && name == "href")
                    keep = safeHttpUrl(value);
                if (tag == "img" && name == "src") {
                    const QString prefix = "clipper://asset/";
                    const QString id = value.mid(prefix.size());
                    static const QRegularExpression assetId("^[A-Za-z0-9_-]{1,128}$");
                    keep = value.startsWith(prefix) && assetId.match(id).hasMatch()
                        && ownedAssetIds.contains(id);
                    removeImage = !keep;
                }
                if (!keep)
                    xmlRemoveProp(attr);
                attr = attrNext;
            }
            if (tag == "img" && (removeImage || !xmlHasProp(current, BAD_CAST "src"))) {
                xmlUnlinkNode(current);
                xmlFreeNode(current);
                current = next;
                continue;
            }
        }
        sanitizeMarkdownNode(current->children, ownedAssetIds);
        current = next;
    }
}

QString serializeChildren(xmlDocPtr document, xmlNodePtr parent)
{
    xmlBufferPtr buffer = xmlBufferCreate();
    if (!buffer)
        throw std::runtime_error("HTML could not be serialized");
    for (xmlNodePtr child = parent->children; child; child = child->next)
        htmlNodeDump(buffer, document, child);
    const QString result = QString::fromUtf8(
        reinterpret_cast<const char *>(xmlBufferContent(buffer)), xmlBufferLength(buffer));
    xmlBufferFree(buffer);
    return result;
}

void collectCodeBlocks(xmlNodePtr node, std::vector<xmlNodePtr> &blocks)
{
    for (xmlNodePtr current = node; current; current = current->next) {
        if (current->type == XML_ELEMENT_NODE
            && xmlString(current->name).compare("pre", Qt::CaseInsensitive) == 0) {
            blocks.push_back(current);
        }
        collectCodeBlocks(current->children, blocks);
    }
}

QString codeLanguageHint(xmlNodePtr code)
{
    xmlChar *raw = xmlGetProp(code, BAD_CAST "class");
    const QString value = xmlString(raw);
    xmlFree(raw);
    if (value.startsWith("language-"))
        return value.mid(9);
    if (value.startsWith("lang-"))
        return value.mid(5);
    return {};
}

QString codeCard(const QString &source, const QString &languageHint,
                 bool allowExternalHighlighter)
{
    const auto highlighted = clipper::highlightCode(source, languageHint, allowExternalHighlighter);
    QStringList openSpans;
    QStringList highlightedLines;
    QString line;
    for (qsizetype index = 0; index < highlighted.html.size();) {
        if (highlighted.html.mid(index, 5) == "<span") {
            const qsizetype end = highlighted.html.indexOf('>', index);
            if (end < 0)
                break;
            const QString tag = highlighted.html.mid(index, end - index + 1);
            openSpans.append(tag);
            line += tag;
            index = end + 1;
        } else if (highlighted.html.mid(index, 7) == "</span>") {
            line += "</span>";
            if (!openSpans.isEmpty())
                openSpans.removeLast();
            index += 7;
        } else if (highlighted.html.at(index) == '\n') {
            for (qsizetype depth = openSpans.size(); depth > 0; --depth)
                line += "</span>";
            highlightedLines.append(line);
            line = openSpans.join(QString());
            ++index;
        } else {
            line += highlighted.html.at(index);
            ++index;
        }
    }
    for (qsizetype depth = openSpans.size(); depth > 0; --depth)
        line += "</span>";
    highlightedLines.append(line);

    QString rows;
    for (qsizetype index = 0; index < highlightedLines.size(); ++index) {
        const QString displayed = highlightedLines.at(index).isEmpty()
            ? QStringLiteral("&#8203;") : highlightedLines.at(index);
        rows += "<span class=\"code-line\"><span class=\"code-line-number\" aria-hidden=\"true\">"
            + QString::number(index + 1) + "</span><span class=\"code-text\">" + displayed
            + "</span></span>";
    }
    const QString known = highlighted.knownLanguage ? "true" : "false";
    const QString wrap = QCoreApplication::translate("ReaderResources", "Wrap").toHtmlEscaped();
    const QString copy = QCoreApplication::translate("ReaderResources", "Copy").toHtmlEscaped();
    return "<div class=\"code-card\" data-language-known=\"" + known + "\">"
        "<div class=\"code-header\"><span class=\"code-language\">"
        + highlighted.displayLanguage.toHtmlEscaped()
        + "</span><span class=\"code-actions\"><button class=\"code-wrap\" type=\"button\" aria-pressed=\"false\">"
        + wrap + "</button><button class=\"code-copy\" type=\"button\">" + copy
        + "</button></span></div>"
          "<textarea class=\"code-original\" aria-hidden=\"true\" tabindex=\"-1\">x"
        + source.toHtmlEscaped()
        + "</textarea><div class=\"code-scroll\"><pre class=\"code-source\"><code>" + rows
        + "</code></pre></div></div>";
}

} // namespace

QString sanitizeCapturedHtml(const QString &html, const QSet<QString> &ownedAssetIds)
{
    if (html.trimmed().isEmpty())
        return {};

    const QByteArray wrapped = QByteArray("<div>") + html.toUtf8() + "</div>";
    htmlDocPtr document = htmlReadMemory(wrapped.constData(), wrapped.size(), nullptr, "UTF-8",
                                         HTML_PARSE_RECOVER | HTML_PARSE_NOERROR |
                                             HTML_PARSE_NOWARNING | HTML_PARSE_NONET);
    if (!document)
        throw std::runtime_error("Captured HTML could not be parsed");

    try {
        xmlNodePtr root = xmlDocGetRootElement(document);
        xmlNodePtr body = findElement(root, "body");
        if (!body)
            throw std::runtime_error("Captured HTML has no body");
        xmlNodePtr wrapper = body->children;
        while (wrapper && wrapper->type != XML_ELEMENT_NODE)
            wrapper = wrapper->next;
        if (!wrapper)
            throw std::runtime_error("Captured HTML is empty");

        rejectActiveContent(wrapper->children);
        sanitizeNode(wrapper->children, ownedAssetIds);

        const QString result = serializeChildren(document, wrapper);
        xmlFreeDoc(document);
        return result;
    } catch (...) {
        xmlFreeDoc(document);
        throw;
    }
}

QString sanitizeMarkdownHtml(const QString &html, const QSet<QString> &ownedAssetIds)
{
    if (html.trimmed().isEmpty())
        return {};

    const QByteArray encoded = html.toUtf8();
    htmlDocPtr document = htmlReadMemory(encoded.constData(), encoded.size(), nullptr, "UTF-8",
                                         HTML_PARSE_RECOVER | HTML_PARSE_NOERROR |
                                             HTML_PARSE_NOWARNING | HTML_PARSE_NONET);
    if (!document)
        return {};
    try {
        xmlNodePtr body = findElement(xmlDocGetRootElement(document), "body");
        if (!body) {
            xmlFreeDoc(document);
            return {};
        }
        sanitizeMarkdownNode(body->children, ownedAssetIds);
        const QString result = serializeChildren(document, body);
        xmlFreeDoc(document);
        return result;
    } catch (...) {
        xmlFreeDoc(document);
        return {};
    }
}

QString decorateReaderCodeBlocks(const QString &html)
{
    if (html.trimmed().isEmpty())
        return {};

    const QByteArray wrapped = QByteArray("<div>") + html.toUtf8() + "</div>";
    htmlDocPtr document = htmlReadMemory(wrapped.constData(), wrapped.size(), nullptr, "UTF-8",
                                         HTML_PARSE_RECOVER | HTML_PARSE_NOERROR |
                                             HTML_PARSE_NOWARNING | HTML_PARSE_NONET);
    if (!document)
        return html;
    try {
        xmlNodePtr body = findElement(xmlDocGetRootElement(document), "body");
        xmlNodePtr wrapper = body ? body->children : nullptr;
        while (wrapper && wrapper->type != XML_ELEMENT_NODE)
            wrapper = wrapper->next;
        if (!wrapper) {
            xmlFreeDoc(document);
            return html;
        }

        std::vector<xmlNodePtr> blocks;
        collectCodeBlocks(wrapper->children, blocks);
        QElapsedTimer highlightingBudget;
        highlightingBudget.start();
        for (xmlNodePtr pre : blocks) {
            constexpr qint64 maximumHighlightingMilliseconds = 350;
            xmlNodePtr code = pre->children;
            while (code && code->type != XML_ELEMENT_NODE)
                code = code->next;
            if (code && xmlString(code->name).compare("code", Qt::CaseInsensitive) != 0)
                code = nullptr;
            xmlChar *raw = xmlNodeGetContent(code ? code : pre);
            const QString source = xmlString(raw);
            xmlFree(raw);
            const QByteArray card = (QByteArray("<div>")
                                     + codeCard(source, code ? codeLanguageHint(code) : QString(),
                                                highlightingBudget.elapsed()
                                                    < maximumHighlightingMilliseconds)
                                           .toUtf8()
                                     + "</div>");
            htmlDocPtr cardDocument = htmlReadMemory(card.constData(), card.size(), nullptr, "UTF-8",
                                                     HTML_PARSE_RECOVER | HTML_PARSE_NOERROR |
                                                         HTML_PARSE_NOWARNING | HTML_PARSE_NONET);
            if (!cardDocument)
                continue;
            xmlNodePtr cardBody = findElement(xmlDocGetRootElement(cardDocument), "body");
            xmlNodePtr cardWrapper = cardBody ? cardBody->children : nullptr;
            while (cardWrapper && cardWrapper->type != XML_ELEMENT_NODE)
                cardWrapper = cardWrapper->next;
            xmlNodePtr replacement = cardWrapper ? cardWrapper->children : nullptr;
            while (replacement && replacement->type != XML_ELEMENT_NODE)
                replacement = replacement->next;
            if (replacement) {
                xmlNodePtr copied = xmlDocCopyNode(replacement, document, 1);
                xmlReplaceNode(pre, copied);
                xmlFreeNode(pre);
            }
            xmlFreeDoc(cardDocument);
        }
        const QString result = serializeChildren(document, wrapper);
        xmlFreeDoc(document);
        return result;
    } catch (...) {
        xmlFreeDoc(document);
        return html;
    }
}
