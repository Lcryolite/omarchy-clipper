#include "runtime_backend.h"
#include "code_highlight.h"
#include "language.h"
#include "theme.h"

#include <QAbstractTextDocumentLayout>
#include <QApplication>
#include <QBuffer>
#include <QClipboard>
#include <QCloseEvent>
#include <QColor>
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFont>
#include <QElapsedTimer>
#include <QHBoxLayout>
#include <QHash>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QMainWindow>
#include <QMessageBox>
#include <QMimeData>
#include <QMimeDatabase>
#include <QMouseEvent>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollArea>
#include <QScrollBar>
#include <QScreen>
#include <QSet>
#include <QStatusBar>
#include <QTextBrowser>
#include <QTextBlock>
#include <QTextCursor>
#include <QTextDocument>
#include <QTextImageFormat>
#include <QTextLayout>
#include <QTextEdit>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>
#include <QWidget>

#include <cstdio>
#include <functional>
#include <stdexcept>

namespace {

QString uiText(const char *source)
{
    return QCoreApplication::translate("NativeMixedWindow", source);
}

QString responseError(const QJsonObject &response)
{
    return localizedUserMessage(
        response.value(QStringLiteral("error")).toString(QStringLiteral("Unknown collection error")));
}

class OwnedAssetBrowser final : public QTextBrowser
{
public:
    explicit OwnedAssetBrowser(RuntimeBackend &backend, QWidget *parent = nullptr)
        : QTextBrowser(parent)
        , m_backend(backend)
    {
        setOpenExternalLinks(false);
        setOpenLinks(false);
        setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    }

    void setOwnedAssets(const QJsonArray &assets)
    {
        m_ownedAssetIds.clear();
        for (const QJsonValue &value : assets) {
            const QString id = value.toObject().value(QStringLiteral("id")).toString();
            if (!id.isEmpty())
                m_ownedAssetIds.insert(id);
        }
        m_decodedAssetIds.clear();
        m_decodedSizes.clear();
        m_rejectedResources.clear();
    }

    int decodedImageCount() const { return m_decodedAssetIds.size(); }
    int rejectedResourceCount() const { return m_rejectedResources.size(); }
    int decodeCount() const { return m_decodeCount; }
    int widestDecodedImage() const
    {
        int width = 0;
        for (const QSize &size : m_decodedSizes)
            width = qMax(width, size.width());
        return width;
    }

    void setImageActivatedHandler(std::function<void(const QString &)> handler)
    {
        m_imageActivated = std::move(handler);
    }

    bool rejectsRemoteResource()
    {
        return !loadResource(QTextDocument::ImageResource,
                             QUrl(QStringLiteral("https://example.invalid/remote.png")))
                    .isValid();
    }

    bool rejectsLocalResource()
    {
        return !loadResource(QTextDocument::ImageResource,
                             QUrl::fromLocalFile(QStringLiteral("/etc/passwd")))
                    .isValid();
    }

protected:
    void mouseReleaseEvent(QMouseEvent *event) override
    {
        if (event->button() == Qt::LeftButton) {
            const QPointF documentPoint = event->position()
                + QPointF(horizontalScrollBar()->value(), verticalScrollBar()->value());
            const QUrl url(document()->documentLayout()->imageAt(documentPoint));
            if (isOwnedAssetUrl(url)) {
                if (m_imageActivated)
                    m_imageActivated(url.path().mid(1));
                event->accept();
                return;
            }
        }
        QTextBrowser::mouseReleaseEvent(event);
    }

    QVariant loadResource(int type, const QUrl &url) override
    {
        if (type != QTextDocument::ImageResource || !isOwnedAssetUrl(url)) {
            m_rejectedResources.insert(url.toString(QUrl::FullyEncoded));
            return {};
        }

        const QString id = url.path().mid(1);
        const QByteArray bytes = m_backend.readAsset(id);
        QBuffer buffer;
        buffer.setData(bytes);
        if (bytes.isEmpty() || !buffer.open(QIODevice::ReadOnly)) {
            m_rejectedResources.insert(url.toString(QUrl::FullyEncoded));
            return {};
        }

        QImageReader reader(&buffer);
        reader.setAutoTransform(true);
        reader.setDecideFormatFromContent(true);
        const QSize sourceSize = reader.size();
        const int widthLimit = qBound(96, viewport()->width() - 32, 1600);
        const QSize decodeLimit(widthLimit, 2048);
        if (sourceSize.isValid()
            && (sourceSize.width() > decodeLimit.width()
                || sourceSize.height() > decodeLimit.height())) {
            reader.setScaledSize(sourceSize.scaled(decodeLimit, Qt::KeepAspectRatio));
        }

        const QImage image = reader.read();
        if (image.isNull()) {
            m_rejectedResources.insert(url.toString(QUrl::FullyEncoded));
            return {};
        }
        m_decodedAssetIds.insert(id);
        m_decodedSizes.insert(id, image.size());
        ++m_decodeCount;
        return image;
    }

private:
    bool isOwnedAssetUrl(const QUrl &url) const
    {
        if (!url.isValid() || url.scheme() != QStringLiteral("clipper")
            || url.host() != QStringLiteral("asset") || url.hasQuery() || url.hasFragment()
            || !url.userInfo().isEmpty() || url.port() != -1) {
            return false;
        }
        const QString path = url.path();
        if (!path.startsWith(QLatin1Char('/')) || path.indexOf(QLatin1Char('/'), 1) != -1)
            return false;
        const QString id = path.mid(1);
        static const QRegularExpression validId(QStringLiteral("^[A-Za-z0-9_-]{1,128}$"));
        return validId.match(id).hasMatch() && m_ownedAssetIds.contains(id);
    }

    RuntimeBackend &m_backend;
    QSet<QString> m_ownedAssetIds;
    QSet<QString> m_decodedAssetIds;
    QHash<QString, QSize> m_decodedSizes;
    QSet<QString> m_rejectedResources;
    int m_decodeCount = 0;
    std::function<void(const QString &)> m_imageActivated;
};

struct ReadingPart {
    bool code = false;
    QString text;
    QString language;
};

QList<ReadingPart> splitMarkdownCodeBlocks(const QString &markdown)
{
    QList<ReadingPart> parts;
    const QRegularExpression opening(
        QStringLiteral(R"(^ {0,3}(`{3,}|~{3,})[ \t]*([^\r\n]*)\r?$)"));
    qsizetype proseStart = 0;
    qsizetype lineStart = 0;
    while (lineStart < markdown.size()) {
        const qsizetype newline = markdown.indexOf(QLatin1Char('\n'), lineStart);
        const qsizetype nextLine = newline < 0 ? markdown.size() : newline + 1;
        const QString line = markdown.mid(lineStart, nextLine - lineStart - (newline < 0 ? 0 : 1));
        const QRegularExpressionMatch match = opening.match(line);
        if (!match.hasMatch()) {
            lineStart = nextLine;
            continue;
        }

        const QString fence = match.captured(1);
        const QChar fenceCharacter = fence.front();
        const QRegularExpression closing(
            QStringLiteral("^ {0,3}%1{%2,}[ \\t]*\\r?$")
                .arg(QRegularExpression::escape(QString(fenceCharacter)))
                .arg(fence.size()));
        const qsizetype codeStart = nextLine;
        qsizetype closingStart = codeStart;
        qsizetype afterClosing = codeStart;
        bool foundClosing = false;
        while (closingStart < markdown.size()) {
            const qsizetype closingNewline = markdown.indexOf(QLatin1Char('\n'), closingStart);
            afterClosing = closingNewline < 0 ? markdown.size() : closingNewline + 1;
            const QString closingLine = markdown.mid(
                closingStart, afterClosing - closingStart - (closingNewline < 0 ? 0 : 1));
            if (closing.match(closingLine).hasMatch()) {
                foundClosing = true;
                break;
            }
            closingStart = afterClosing;
        }
        if (!foundClosing) {
            lineStart = nextLine;
            continue;
        }

        if (lineStart > proseStart)
            parts.append({false, markdown.mid(proseStart, lineStart - proseStart), {}});
        const QString info = match.captured(2).trimmed();
        parts.append({true, markdown.mid(codeStart, closingStart - codeStart),
                      info.section(QRegularExpression(QStringLiteral("\\s+")), 0, 0)});
        proseStart = afterClosing;
        lineStart = afterClosing;
    }
    if (proseStart < markdown.size())
        parts.append({false, markdown.mid(proseStart), {}});
    if (parts.isEmpty())
        parts.append({false, markdown, {}});
    return parts;
}

class CodeLineNumberArea final : public QWidget
{
public:
    explicit CodeLineNumberArea(QTextEdit *editor, QWidget *parent = nullptr)
        : QWidget(parent)
        , m_editor(editor)
    {
        setObjectName(QStringLiteral("codeLineNumbers"));
        setAttribute(Qt::WA_TransparentForMouseEvents);
        m_editor->viewport()->installEventFilter(this);
        connect(m_editor->verticalScrollBar(), &QScrollBar::valueChanged, this,
                [this] { refresh(); });
        connect(m_editor->document(), &QTextDocument::contentsChanged, this,
                [this] { refresh(); });
        connect(m_editor->document()->documentLayout(),
                &QAbstractTextDocumentLayout::documentSizeChanged, this,
                [this] { refresh(); });
    }

    void configure(const QFont &font, int lineCount)
    {
        m_font = font;
        m_lineHeight = QFontMetrics(font).lineSpacing();
        for (QLabel *label : std::as_const(m_labels))
            label->setFont(font);
        setFixedWidth(QFontMetrics(font).horizontalAdvance(QString::number(lineCount)) + 22);
        refresh();
    }

    void refresh()
    {
        for (QLabel *label : std::as_const(m_labels))
            label->hide();

        int visibleIndex = 0;
        for (QTextBlock block = m_editor->cursorForPosition(QPoint(0, 0)).block();
             block.isValid(); block = block.next()) {
            const QRect cursorRect = m_editor->cursorRect(QTextCursor(block));
            if (cursorRect.top() > height())
                break;
            if (cursorRect.bottom() < 0)
                continue;
            QLabel *label = labelAt(visibleIndex++);
            label->setText(QString::number(block.blockNumber() + 1));
            label->setProperty("sourceLine", block.blockNumber() + 1);
            label->setGeometry(0, cursorRect.top(), width() - 8,
                               qMax(m_lineHeight, cursorRect.height()));
            label->show();
        }
    }

protected:
    bool eventFilter(QObject *watched, QEvent *event) override
    {
        if (watched == m_editor->viewport()
            && (event->type() == QEvent::Resize || event->type() == QEvent::Show)) {
            QTimer::singleShot(0, this, [this] { refresh(); });
        }
        return QWidget::eventFilter(watched, event);
    }

    void resizeEvent(QResizeEvent *event) override
    {
        QWidget::resizeEvent(event);
        refresh();
    }

private:
    QLabel *labelAt(int index)
    {
        while (m_labels.size() <= index) {
            auto *label = new QLabel(this);
            label->setObjectName(QStringLiteral("codeLineNumber"));
            label->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
            label->setTextInteractionFlags(Qt::NoTextInteraction);
            label->setAttribute(Qt::WA_TransparentForMouseEvents);
            label->setFont(m_font);
            m_labels.append(label);
        }
        return m_labels.at(index);
    }

    QTextEdit *m_editor = nullptr;
    QList<QLabel *> m_labels;
    QFont m_font;
    int m_lineHeight = 0;
};

class CodeBlockWidget final : public QWidget
{
public:
    CodeBlockWidget(const QString &source, const QString &languageHint,
                    const QJsonObject &theme, bool allowExternalHighlighter,
                    QWidget *parent = nullptr)
        : QWidget(parent)
        , m_source(source)
    {
        setObjectName(QStringLiteral("codeBlock"));
        auto *outer = new QVBoxLayout(this);
        outer->setContentsMargins(0, 0, 0, 0);
        outer->setSpacing(0);

        auto *header = new QWidget(this);
        header->setObjectName(QStringLiteral("codeHeader"));
        auto *headerLayout = new QHBoxLayout(header);
        headerLayout->setContentsMargins(12, 6, 6, 6);
        headerLayout->setSpacing(6);
        auto *language = new QLabel(header);
        language->setObjectName(QStringLiteral("codeLanguageLabel"));
        const clipper::CodeHighlightResult highlighted =
            clipper::highlightCode(source, languageHint, allowExternalHighlighter);
        language->setText(highlighted.displayLanguage);
        headerLayout->addWidget(language);
        headerLayout->addStretch();
        auto *wrap = new QPushButton(uiText("Wrap"), header);
        auto *copy = new QPushButton(uiText("Copy"), header);
        wrap->setObjectName(QStringLiteral("wrapCodeButton"));
        copy->setObjectName(QStringLiteral("copyCodeButton"));
        headerLayout->addWidget(wrap);
        headerLayout->addWidget(copy);
        outer->addWidget(header);

        auto *body = new QWidget(this);
        auto *bodyLayout = new QHBoxLayout(body);
        bodyLayout->setContentsMargins(0, 0, 0, 0);
        bodyLayout->setSpacing(0);
        const int lineCount = qMax(1, source.count(QLatin1Char('\n')) + 1);
        m_editor = new QTextEdit(body);
        m_editor->setObjectName(QStringLiteral("codeText"));
        m_editor->setReadOnly(true);
        m_editor->setAcceptRichText(false);
        m_editor->setLineWrapMode(QTextEdit::NoWrap);
        m_editor->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
        m_editor->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
        const QString foreground = theme.value(QStringLiteral("foreground")).toString();
        const QString muted = theme.value(QStringLiteral("muted")).toString();
        const QString accent = theme.value(QStringLiteral("accent")).toString();
        const QString html = QStringLiteral(
            "<html><head><style>"
            "pre { margin: 0; white-space: pre; color: %1; }"
            ".normal { color: %1; } .keyword, .preproc { color: %2; font-weight: 600; }"
            ".type, .function { color: #d3b56f; } .string { color: #9fc59a; }"
            ".comment { color: %3; font-style: italic; } .number { color: #d69b7c; }"
            ".symbol, .variable { color: %1; }"
            "</style></head><body><pre>%4</pre></body></html>")
                                 .arg(foreground, accent, muted, highlighted.html);
        if (highlighted.syntaxHighlighted)
            m_editor->setHtml(html);
        else
            m_editor->setPlainText(source);
        QFont mono(QStringLiteral("monospace"));
        mono.setStyleHint(QFont::Monospace);
        mono.setPointSize(11);
        m_editor->setFont(mono);
        m_lineNumbers = new CodeLineNumberArea(m_editor, body);
        m_lineNumbers->configure(mono, lineCount);
        const int height = qBound(116, (lineCount + 1) * QFontMetrics(mono).lineSpacing() + 20, 420);
        m_editor->setFixedHeight(height);
        bodyLayout->addWidget(m_lineNumbers);
        bodyLayout->addWidget(m_editor, 1);
        outer->addWidget(body);

        connect(copy, &QPushButton::clicked, this, [this] {
            QApplication::clipboard()->setText(m_source);
        });
        connect(wrap, &QPushButton::clicked, this, [this, wrap] {
            const bool wrapped = m_editor->lineWrapMode() != QTextEdit::NoWrap;
            m_editor->setLineWrapMode(wrapped ? QTextEdit::NoWrap : QTextEdit::WidgetWidth);
            wrap->setProperty("wrapped", !wrapped);
            wrap->setText(wrapped ? uiText("Wrap") : uiText("Unwrap"));
            QTimer::singleShot(0, m_lineNumbers, [this] { m_lineNumbers->refresh(); });
        });
    }

private:
    QString m_source;
    QTextEdit *m_editor = nullptr;
    CodeLineNumberArea *m_lineNumbers = nullptr;
};

class MixedReadingView final : public QScrollArea
{
public:
    explicit MixedReadingView(RuntimeBackend &backend, QWidget *parent = nullptr)
        : QScrollArea(parent)
        , m_backend(backend)
        , m_content(new QWidget(this))
        , m_layout(new QVBoxLayout(m_content))
    {
        setObjectName(QStringLiteral("mixedReadingView"));
        setWidgetResizable(true);
        setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        setFrameShape(QFrame::NoFrame);
        m_layout->setContentsMargins(22, 18, 22, 28);
        m_layout->setSpacing(18);
        m_layout->setAlignment(Qt::AlignTop);
        setWidget(m_content);
    }

    void setImageActivatedHandler(std::function<void(const QString &)> handler)
    {
        m_imageActivated = std::move(handler);
    }

    void setAnchorActivatedHandler(std::function<void(const QUrl &)> handler)
    {
        m_anchorActivated = std::move(handler);
    }

    void setContent(const QString &markdown, const QJsonArray &assets,
                    const QJsonObject &theme)
    {
        m_markdown = markdown;
        m_assets = assets;
        while (QLayoutItem *item = m_layout->takeAt(0)) {
            if (QWidget *widget = item->widget())
                delete widget;
            delete item;
        }
        m_browsers.clear();

        const QList<ReadingPart> parts = splitMarkdownCodeBlocks(markdown);
        QElapsedTimer highlightingBudget;
        highlightingBudget.start();
        for (const ReadingPart &part : parts) {
            if (part.code) {
                constexpr qint64 maximumHighlightingMilliseconds = 350;
                m_layout->addWidget(new CodeBlockWidget(
                    part.text, part.language, theme,
                    highlightingBudget.elapsed() < maximumHighlightingMilliseconds, m_content));
            } else if (!part.text.trimmed().isEmpty()) {
                addMarkdownBrowser(part.text, theme, false);
            }
        }
        if (m_browsers.isEmpty() && assets.isEmpty())
            addMarkdownBrowser(QString(), theme, false);
        if (!assets.isEmpty())
            addMarkdownBrowser(QString(), theme, true);
        m_layout->addStretch();
        QTimer::singleShot(0, this, [this] { updateBrowserHeights(); });
    }

    void setOwnedAssets(const QJsonArray &assets)
    {
        m_assets = assets;
        for (OwnedAssetBrowser *browser : std::as_const(m_browsers))
            browser->setOwnedAssets(assets);
    }

    QString toPlainText() const { return m_markdown; }

    bool hasLaidOutText() const
    {
        for (OwnedAssetBrowser *browser : m_browsers) {
            for (QTextBlock block = browser->document()->begin(); block.isValid();
                 block = block.next()) {
                if (!block.text().trimmed().isEmpty() && block.layout()
                    && block.layout()->lineCount() > 0) {
                    return true;
                }
            }
        }
        return m_markdown.trimmed().isEmpty();
    }

    int decodedImageCount() const
    {
        int count = 0;
        for (OwnedAssetBrowser *browser : m_browsers)
            count += browser->decodedImageCount();
        return count;
    }

    int rejectedResourceCount() const
    {
        int count = 0;
        for (OwnedAssetBrowser *browser : m_browsers)
            count += browser->rejectedResourceCount();
        return count;
    }

    int decodeCount() const
    {
        int count = 0;
        for (OwnedAssetBrowser *browser : m_browsers)
            count += browser->decodeCount();
        return count;
    }

    int widestDecodedImage() const
    {
        int width = 0;
        for (OwnedAssetBrowser *browser : m_browsers)
            width = qMax(width, browser->widestDecodedImage());
        return width;
    }

    bool rejectsRemoteResource()
    {
        return !m_browsers.isEmpty() && m_browsers.front()->rejectsRemoteResource();
    }

    bool rejectsLocalResource()
    {
        return !m_browsers.isEmpty() && m_browsers.front()->rejectsLocalResource();
    }

    QSizeF contentSize() const { return m_content->size(); }

protected:
    void resizeEvent(QResizeEvent *event) override
    {
        QScrollArea::resizeEvent(event);
        QTimer::singleShot(0, this, [this] { updateBrowserHeights(); });
    }

private:
    void addMarkdownBrowser(const QString &markdown, const QJsonObject &theme, bool images)
    {
        auto *browser = new OwnedAssetBrowser(m_backend, m_content);
        browser->setObjectName(images ? QStringLiteral("mixedImages")
                                      : QStringLiteral("mixedProse"));
        browser->setOwnedAssets(m_assets);
        browser->setFrameShape(QFrame::NoFrame);
        browser->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        browser->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        browser->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        connect(browser->document()->documentLayout(),
                &QAbstractTextDocumentLayout::documentSizeChanged, browser,
                [browser](const QSizeF &size) {
                    const int height = qMax(28, int(size.height() + 0.999) + 4);
                    if (browser->height() != height)
                        browser->setFixedHeight(height);
                });
        browser->document()->setDocumentMargin(0);
        QFont readingFont(QStringLiteral("serif"));
        readingFont.setStyleHint(QFont::Serif);
        readingFont.setPointSize(12);
        browser->document()->setDefaultFont(readingFont);
        browser->document()->setDefaultStyleSheet(
            QStringLiteral(
                "body { color: %1; font-size: 17px; } "
                "h1 { font-size: 29px; margin-top: 8px; margin-bottom: 16px; } "
                "h2 { font-size: 24px; margin-top: 22px; margin-bottom: 12px; } "
                "h3 { font-size: 20px; margin-top: 18px; margin-bottom: 10px; } "
                "p, li { margin-bottom: 10px; } a { color: %2; } "
                "code { font-family: monospace; background-color: %3; } "
                "table { border-collapse: collapse; margin: 12px 0; } "
                "td, th { border: 1px solid %4; padding: 7px; } "
                "blockquote { border-left: 3px solid %2; margin-left: 0; padding-left: 14px; color: %5; }")
                .arg(theme.value(QStringLiteral("foreground")).toString(),
                     theme.value(QStringLiteral("accent")).toString(),
                     theme.value(QStringLiteral("surface")).toString(),
                     theme.value(QStringLiteral("border")).toString(),
                     theme.value(QStringLiteral("muted")).toString()));
        browser->document()->setMarkdown(
            markdown,
            QTextDocument::MarkdownFeatures(QTextDocument::MarkdownDialectGitHub)
                | QTextDocument::MarkdownNoHTML);
        styleInlineCodeAndLinks(browser, theme);
        if (images) {
            QTextCursor cursor(browser->document());
            cursor.movePosition(QTextCursor::End);
            for (const QJsonValue &value : m_assets) {
                const QString id = value.toObject().value(QStringLiteral("id")).toString();
                if (!cursor.atStart())
                    cursor.insertBlock();
                QTextImageFormat image;
                image.setName(QStringLiteral("clipper://asset/%1").arg(id));
                cursor.insertImage(image);
            }
        }
        browser->setImageActivatedHandler([this](const QString &id) {
            if (m_imageActivated)
                m_imageActivated(id);
        });
        connect(browser, &QTextBrowser::anchorClicked, this, [this](const QUrl &url) {
            if (m_anchorActivated)
                m_anchorActivated(url);
        });
        m_browsers.append(browser);
        m_layout->addWidget(browser);
    }

    static void styleInlineCodeAndLinks(OwnedAssetBrowser *browser,
                                        const QJsonObject &theme)
    {
        for (QTextBlock block = browser->document()->begin(); block.isValid();
             block = block.next()) {
            for (QTextBlock::Iterator it = block.begin(); !it.atEnd(); ++it) {
                const QTextFragment fragment = it.fragment();
                if (!fragment.isValid())
                    continue;
                const QTextCharFormat current = fragment.charFormat();
                QTextCharFormat displayFormat;
                bool changed = false;
                if (current.fontFixedPitch()) {
                    displayFormat.setBackground(
                        QColor(theme.value(QStringLiteral("surface")).toString()));
                    changed = true;
                }
                if (current.isAnchor()) {
                    displayFormat.setForeground(
                        QColor(theme.value(QStringLiteral("accent")).toString()));
                    displayFormat.setFontUnderline(true);
                    changed = true;
                }
                if (!changed)
                    continue;
                QTextCursor cursor(browser->document());
                cursor.setPosition(fragment.position());
                cursor.setPosition(fragment.position() + fragment.length(), QTextCursor::KeepAnchor);
                cursor.mergeCharFormat(displayFormat);
            }
        }
    }

    void updateBrowserHeights()
    {
        for (OwnedAssetBrowser *browser : std::as_const(m_browsers)) {
            const int width = qMax(120, browser->viewport()->width());
            browser->document()->setTextWidth(width);
            const int height = qMax(28, int(qCeil(browser->document()->size().height())) + 4);
            browser->setFixedHeight(height);
        }
    }

    RuntimeBackend &m_backend;
    QWidget *m_content = nullptr;
    QVBoxLayout *m_layout = nullptr;
    QList<OwnedAssetBrowser *> m_browsers;
    QJsonArray m_assets;
    QString m_markdown;
    std::function<void(const QString &)> m_imageActivated;
    std::function<void(const QUrl &)> m_anchorActivated;
};

class ImageScrollArea final : public QScrollArea
{
public:
    explicit ImageScrollArea(QWidget *parent = nullptr)
        : QScrollArea(parent)
        , m_label(new QLabel(this))
    {
        setObjectName(QStringLiteral("imageScrollArea"));
        setWidgetResizable(false);
        setAlignment(Qt::AlignCenter);
        setFocusPolicy(Qt::StrongFocus);
        m_label->setObjectName(QStringLiteral("fullSizeImage"));
        m_label->setAlignment(Qt::AlignCenter);
        m_label->setAttribute(Qt::WA_TransparentForMouseEvents);
        setWidget(m_label);
    }

    void setImage(const QImage &image)
    {
        m_image = image;
        updateImage();
    }

    void setFit(bool fit)
    {
        if (m_fit == fit)
            return;
        m_fit = fit;
        updateImage();
    }

    void releaseImage()
    {
        m_label->clear();
        m_label->resize(0, 0);
        m_image = QImage();
    }

    void updateImage()
    {
        if (m_image.isNull())
            return;
        const QImage displayed = m_fit
            ? m_image.scaled(viewport()->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation)
            : m_image;
        m_label->setPixmap(QPixmap::fromImage(displayed));
        m_label->resize(displayed.size());
        if (!m_fit) {
            QTimer::singleShot(0, this, [this] {
                if (!m_fit) {
                    horizontalScrollBar()->setValue(horizontalScrollBar()->maximum() / 2);
                    verticalScrollBar()->setValue(verticalScrollBar()->maximum() / 2);
                    updateCursor();
                }
            });
        }
        updateCursor();
    }

protected:
    void resizeEvent(QResizeEvent *event) override
    {
        QScrollArea::resizeEvent(event);
        if (m_fit)
            updateImage();
        else
            updateCursor();
    }

    void mousePressEvent(QMouseEvent *event) override
    {
        if (event->button() == Qt::LeftButton && canPan()) {
            m_dragging = true;
            m_dragStart = event->position().toPoint();
            m_horizontalStart = horizontalScrollBar()->value();
            m_verticalStart = verticalScrollBar()->value();
            viewport()->setCursor(Qt::ClosedHandCursor);
            event->accept();
            return;
        }
        QScrollArea::mousePressEvent(event);
    }

    void mouseMoveEvent(QMouseEvent *event) override
    {
        if (m_dragging) {
            const QPoint delta = event->position().toPoint() - m_dragStart;
            horizontalScrollBar()->setValue(m_horizontalStart - delta.x());
            verticalScrollBar()->setValue(m_verticalStart - delta.y());
            event->accept();
            return;
        }
        QScrollArea::mouseMoveEvent(event);
    }

    void mouseReleaseEvent(QMouseEvent *event) override
    {
        if (event->button() == Qt::LeftButton && m_dragging) {
            m_dragging = false;
            updateCursor();
            event->accept();
            return;
        }
        QScrollArea::mouseReleaseEvent(event);
    }

private:
    bool canPan() const
    {
        return !m_fit
            && (horizontalScrollBar()->maximum() > 0
                || verticalScrollBar()->maximum() > 0);
    }

    void updateCursor()
    {
        viewport()->setCursor(canPan() ? Qt::OpenHandCursor : Qt::ArrowCursor);
    }

    QLabel *m_label = nullptr;
    QImage m_image;
    bool m_fit = true;
    bool m_dragging = false;
    QPoint m_dragStart;
    int m_horizontalStart = 0;
    int m_verticalStart = 0;
};

class NativeMixedWindow final : public QMainWindow
{
public:
    NativeMixedWindow(const QString &dataDir, const QString &clipId,
                      const QString &backend)
        : m_language()
        , m_backend(dataDir, backend)
        , m_clipId(clipId)
    {
        setWindowTitle(uiText("Clipper — Detail"));
        resize(1120, 760);
        setMinimumSize(360, 360);

        auto *central = new QWidget(this);
        auto *layout = new QVBoxLayout(central);
        layout->setContentsMargins(16, 12, 16, 16);
        layout->setSpacing(10);
        auto *toolbar = new QVBoxLayout;
        toolbar->setSpacing(8);
        auto *toolbarActions = new QHBoxLayout;
        toolbarActions->setSpacing(8);
        m_titleLabel = new QLabel(QStringLiteral("Clipper"), central);
        m_titleLabel->setTextFormat(Qt::PlainText);
        QFont titleFont = m_titleLabel->font();
        titleFont.setBold(true);
        m_titleLabel->setFont(titleFont);
        m_copyButton = new QPushButton(uiText("Copy clip"), central);
        m_reloadButton = new QPushButton(uiText("Reload"), central);
        m_editButton = new QPushButton(uiText("Edit"), central);
        m_addFileButton = new QPushButton(uiText("Add image"), central);
        m_pasteImageButton = new QPushButton(uiText("Paste image"), central);
        m_cancelButton = new QPushButton(uiText("Cancel"), central);
        m_saveButton = new QPushButton(uiText("Save"), central);
        m_closeButton = new QPushButton(uiText("Close"), central);
        m_copyButton->setObjectName(QStringLiteral("copyClipButton"));
        m_reloadButton->setObjectName(QStringLiteral("reloadMixedButton"));
        m_editButton->setObjectName(QStringLiteral("editMixedButton"));
        m_addFileButton->setObjectName(QStringLiteral("addImageButton"));
        m_pasteImageButton->setObjectName(QStringLiteral("pasteImageButton"));
        m_cancelButton->setObjectName(QStringLiteral("cancelMixedButton"));
        m_saveButton->setObjectName(QStringLiteral("saveMixedButton"));
        m_closeButton->setObjectName(QStringLiteral("closeMixedButton"));
        toolbar->addWidget(m_titleLabel);
        toolbarActions->addStretch();
        toolbarActions->addWidget(m_copyButton);
        toolbarActions->addWidget(m_reloadButton);
        toolbarActions->addWidget(m_editButton);
        toolbarActions->addWidget(m_addFileButton);
        toolbarActions->addWidget(m_pasteImageButton);
        toolbarActions->addWidget(m_cancelButton);
        toolbarActions->addWidget(m_saveButton);
        toolbarActions->addWidget(m_closeButton);
        toolbar->addLayout(toolbarActions);
        layout->addLayout(toolbar);

        m_editor = new QPlainTextEdit(central);
        m_editor->setObjectName(QStringLiteral("mixedEditor"));
        m_editor->setPlaceholderText(uiText("Edit Markdown"));
        m_editor->setLineWrapMode(QPlainTextEdit::WidgetWidth);
        m_preview = new MixedReadingView(m_backend, central);
        layout->addWidget(m_editor, 1);
        layout->addWidget(m_preview, 1);
        m_attachmentScroll = new QScrollArea(central);
        m_attachmentScroll->setObjectName(QStringLiteral("attachmentActions"));
        m_attachmentScroll->setWidgetResizable(true);
        m_attachmentScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        m_attachmentScroll->setMaximumHeight(200);
        m_attachmentPanel = new QWidget(m_attachmentScroll);
        m_attachmentLayout = new QVBoxLayout(m_attachmentPanel);
        m_attachmentLayout->setContentsMargins(0, 0, 0, 0);
        m_attachmentLayout->setSpacing(6);
        m_attachmentLayout->setSizeConstraint(QLayout::SetMinAndMaxSize);
        m_attachmentScroll->setWidget(m_attachmentPanel);
        layout->addWidget(m_attachmentScroll);
        setCentralWidget(central);

        m_resizeRefresh.setSingleShot(true);
        m_resizeRefresh.setInterval(80);
        m_themeRefresh.setInterval(30000);
        m_preview->viewport()->installEventFilter(this);
        connect(&m_language, &LanguageSettings::languageChanged,
                this, [this] { retranslateUi(); });
        connect(&m_resizeRefresh, &QTimer::timeout, this, [this] { refreshPreview(); });
        connect(&m_themeRefresh, &QTimer::timeout, this, [this] {
            applyTheme();
            refreshPreview();
        });
        connect(m_editor, &QPlainTextEdit::textChanged, this, [this] {
            if (!m_loading)
                updateActions();
        });
        connect(m_copyButton, &QPushButton::clicked, this, [this] {
            QApplication::clipboard()->setText(m_savedText);
            statusBar()->showMessage(uiText("Clip copied"), 2500);
        });
        connect(m_reloadButton, &QPushButton::clicked, this, [this] {
            if (resolveUnsavedChanges("reload this clip"))
                load();
        });
        connect(m_editButton, &QPushButton::clicked, this, [this] { setEditing(true); });
        connect(m_addFileButton, &QPushButton::clicked, this, [this] { addImageFromFile(); });
        connect(m_pasteImageButton, &QPushButton::clicked, this,
                [this] { addImageFromClipboard(); });
        connect(m_cancelButton, &QPushButton::clicked, this, [this] { cancelEditing(); });
        connect(m_saveButton, &QPushButton::clicked, this, [this] { save(); });
        connect(m_closeButton, &QPushButton::clicked, this, &QWidget::close);
        m_preview->setImageActivatedHandler([this](const QString &id) { showImage(id); });
        m_preview->setAnchorActivatedHandler([this](const QUrl &url) {
            const QUrl strict(url.toString(), QUrl::StrictMode);
            const bool webLink = strict.isValid()
                && (strict.scheme() == QStringLiteral("http")
                    || strict.scheme() == QStringLiteral("https"))
                && !strict.host().isEmpty() && strict.userInfo().isEmpty();
            if (!webLink) {
                statusBar()->showMessage(uiText("Blocked non-HTTP(S) link"), 5000);
                return;
            }
            if (!QDesktopServices::openUrl(strict))
                statusBar()->showMessage(uiText("Could not open link"), 5000);
        });

        applyTheme();
        load();
        m_themeRefresh.start();
    }

    bool eventFilter(QObject *watched, QEvent *event) override
    {
        if (watched == m_preview->viewport() && event->type() == QEvent::Resize && !m_loading)
            m_resizeRefresh.start();
        return QMainWindow::eventFilter(watched, event);
    }

    void closeEvent(QCloseEvent *event) override
    {
        if (resolveUnsavedChanges("close this window"))
            event->accept();
        else
            event->ignore();
    }

#ifdef CLIPPER_NATIVE_MIXED_DEVELOPMENT
#include "../prototypes/native-mixed/native_detail_development.inc"
#endif

private:

    bool isDirty() const
    {
        return m_editor->toPlainText() != m_savedText
            || assetIds(m_assets) != assetIds(m_savedAssets);
    }

    void setEditing(bool editing)
    {
        m_editing = editing;
        m_editor->setVisible(editing);
        m_preview->setVisible(!editing);
        if (editing) {
            m_editor->setFocus();
            m_editor->moveCursor(QTextCursor::End);
        }
        updateActions();
        rebuildAttachmentActions();
    }

    void cancelEditing()
    {
        m_loading = true;
        m_editor->setPlainText(m_savedText);
        m_assets = m_savedAssets;
        updateOwnedAssets();
        m_loading = false;
        setEditing(false);
        statusBar()->showMessage(uiText("Changes discarded"), 2500);
    }

    void updateActions()
    {
        const bool dirty = isDirty();
        m_copyButton->setVisible(!m_editing);
        m_editButton->setVisible(!m_editing);
        m_reloadButton->setVisible(!m_editing);
        m_addFileButton->setVisible(m_editing);
        m_pasteImageButton->setVisible(m_editing);
        m_cancelButton->setVisible(m_editing);
        m_saveButton->setVisible(m_editing);
        m_saveButton->setEnabled(m_editing && dirty);
        if (m_editing)
            statusBar()->showMessage(dirty ? uiText("Unsaved changes")
                                           : uiText("Markdown editor"));
    }

    bool findAsset(const QString &id, QJsonObject *result) const
    {
        for (const QJsonValue &value : m_assets) {
            const QJsonObject asset = value.toObject();
            if (asset.value(QStringLiteral("id")).toString() == id) {
                *result = asset;
                return true;
            }
        }
        return false;
    }

    void updateOwnedAssets()
    {
        m_preview->setOwnedAssets(m_assets);
    }

    bool importImage(const QByteArray &bytes, const QString &mime, const QString &name)
    {
        if (bytes.isEmpty()) {
            statusBar()->showMessage(uiText("Image data is empty"), 10000);
            return false;
        }
        const QJsonObject response = m_backend.dispatch(
            {{QStringLiteral("op"), QStringLiteral("asset.import")},
             {QStringLiteral("data"), QString::fromLatin1(bytes.toBase64())},
             {QStringLiteral("mime"), mime},
             {QStringLiteral("name"), name}});
        if (!response.value(QStringLiteral("ok")).toBool()) {
            statusBar()->showMessage(
                uiText("Image import failed: %1").arg(responseError(response)), 10000);
            return false;
        }
        const QJsonObject asset = response.value(QStringLiteral("result")).toObject();
        const QString id = asset.value(QStringLiteral("id")).toString();
        for (const QJsonValue &value : m_assets) {
            if (value.toObject().value(QStringLiteral("id")).toString() == id) {
                statusBar()->showMessage(uiText("Image is already attached"), 5000);
                return true;
            }
        }
        m_assets.append(asset);
        updateOwnedAssets();
        updateActions();
        rebuildAttachmentActions();
        statusBar()->showMessage(uiText("Image added — save to keep changes"), 5000);
        return true;
    }

    void addImageFromFile()
    {
        QFileDialog dialog(this);
        dialog.setObjectName(QStringLiteral("addImageDialog"));
        dialog.setFileMode(QFileDialog::ExistingFile);
        dialog.setOption(QFileDialog::DontUseNativeDialog);
        const auto retranslateDialog = [&dialog] {
            const QStringList selected = dialog.selectedFiles();
            dialog.setWindowTitle(uiText("Add image"));
            dialog.setNameFilter(uiText("Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp)"));
            dialog.setLabelText(QFileDialog::LookIn, uiText("Look in:"));
            dialog.setLabelText(QFileDialog::FileName, uiText("File name:"));
            dialog.setLabelText(QFileDialog::FileType, uiText("Files of type:"));
            dialog.setLabelText(QFileDialog::Accept, uiText("Open"));
            dialog.setLabelText(QFileDialog::Reject, uiText("Cancel"));
            if (!selected.isEmpty())
                dialog.selectFile(selected.first());
        };
        connect(&m_language, &LanguageSettings::languageChanged, &dialog,
                [&dialog, retranslateDialog] {
                    retranslateDialog();
                    QTimer::singleShot(0, &dialog, retranslateDialog);
                });
        retranslateDialog();
        if (dialog.exec() != QDialog::Accepted || dialog.selectedFiles().isEmpty())
            return;
        QFile file(dialog.selectedFiles().first());
        if (!file.open(QIODevice::ReadOnly) || file.size() > 12 * 1024 * 1024) {
            statusBar()->showMessage(
                uiText("Image could not be read or exceeds 12 MiB"), 10000);
            return;
        }
        const QByteArray bytes = file.readAll();
        const QString mime = QMimeDatabase().mimeTypeForData(bytes).name();
        importImage(bytes, mime, QFileInfo(file).fileName());
    }

    void addImageFromClipboard()
    {
        const QMimeData *data = QApplication::clipboard()->mimeData();
        const QStringList supported{QStringLiteral("image/png"), QStringLiteral("image/jpeg"),
                                    QStringLiteral("image/webp"), QStringLiteral("image/gif"),
                                    QStringLiteral("image/bmp")};
        for (const QString &mime : supported) {
            if (data && data->hasFormat(mime) && !data->data(mime).isEmpty()) {
                importImage(data->data(mime), mime, QStringLiteral("clipboard-image"));
                return;
            }
        }
        if (data && data->hasImage()) {
            const QImage image = qvariant_cast<QImage>(data->imageData());
            QByteArray bytes;
            QBuffer buffer(&bytes);
            if (buffer.open(QIODevice::WriteOnly) && image.save(&buffer, "PNG")) {
                importImage(bytes, QStringLiteral("image/png"),
                            QStringLiteral("clipboard-image.png"));
                return;
            }
        }
        statusBar()->showMessage(uiText("Clipboard does not contain a supported image"),
                                 10000);
    }

    void removeImage(const QString &id)
    {
        QJsonArray retained;
        for (const QJsonValue &value : m_assets) {
            if (value.toObject().value(QStringLiteral("id")).toString() != id)
                retained.append(value);
        }
        m_assets = retained;
        updateOwnedAssets();
        updateActions();
        rebuildAttachmentActions();
        statusBar()->showMessage(uiText("Image removed — save to keep changes"), 5000);
    }

    bool readOwnedImage(const QString &id, QByteArray *bytes, QImage *image, QString *mime,
                        QString *error) const
    {
        QJsonObject asset;
        if (!findAsset(id, &asset)) {
            *error = uiText("Image does not belong to this clip");
            return false;
        }
        QString readError;
        const QByteArray original = m_backend.readAsset(id, nullptr, &readError);
        if (original.isEmpty()) {
            *error = uiText("Image is missing");
            return false;
        }
        QBuffer buffer;
        buffer.setData(original);
        if (!buffer.open(QIODevice::ReadOnly)) {
            *error = uiText("Image could not be read");
            return false;
        }
        QImageReader reader(&buffer);
        reader.setAutoTransform(true);
        reader.setDecideFormatFromContent(true);
        const QImage decoded = reader.read();
        if (decoded.isNull()) {
            *error = uiText("Image could not be decoded");
            return false;
        }
        if (bytes)
            *bytes = original;
        if (image)
            *image = decoded;
        if (mime)
            *mime = asset.value(QStringLiteral("mime")).toString();
        return true;
    }

    QString inspectOwnedImage(const QString &id) const
    {
        QJsonObject asset;
        if (!findAsset(id, &asset))
            return uiText("Image does not belong to this clip");
        const QByteArray bytes = m_backend.readAsset(id);
        if (bytes.isEmpty())
            return uiText("Image is missing");
        QBuffer buffer;
        buffer.setData(bytes);
        if (!buffer.open(QIODevice::ReadOnly))
            return uiText("Image could not be read");
        QImageReader reader(&buffer);
        reader.setDecideFormatFromContent(true);
        return reader.canRead() ? QString() : uiText("Image could not be decoded");
    }

    bool showImage(const QString &id)
    {
        if (m_imageDialog) {
            m_imageDialog->raise();
            m_imageDialog->activateWindow();
            return m_imageDialog->property("assetId").toString() == id;
        }
        QImage image;
        QString error;
        if (!readOwnedImage(id, nullptr, &image, nullptr, &error)) {
            statusBar()->showMessage(error, 10000);
            return false;
        }

        auto *dialog = new QDialog(this);
        dialog->setObjectName(QStringLiteral("imageDialog"));
        dialog->setProperty("assetId", id);
        dialog->setWindowTitle(uiText("Clipper — Image"));
        dialog->setWindowModality(Qt::WindowModal);
        const QSize available = screen() ? screen()->availableGeometry().size() : QSize(1100, 800);
        const int maximumWidth = qMin(1100, qMax(320, available.width() - 80));
        const int maximumHeight = qMin(800, qMax(240, available.height() - 80));
        dialog->setMinimumSize(qMin(280, maximumWidth), qMin(200, maximumHeight));
        dialog->resize(qMin(qMax(image.width() + 48, 480), maximumWidth),
                       qMin(qMax(image.height() + 96, 360), maximumHeight));
        auto *layout = new QVBoxLayout(dialog);
        auto *scroll = new ImageScrollArea(dialog);
        scroll->setImage(image);
        layout->addWidget(scroll, 1);
        auto *buttons = new QDialogButtonBox(QDialogButtonBox::Close, dialog);
        auto *sizeToggle = buttons->addButton(uiText("Actual size"),
                                              QDialogButtonBox::ActionRole);
        sizeToggle->setObjectName(QStringLiteral("imageSizeToggleButton"));
        buttons->button(QDialogButtonBox::Close)->setText(uiText("Close"));
        buttons->button(QDialogButtonBox::Close)
            ->setObjectName(QStringLiteral("closeImageButton"));
        connect(sizeToggle, &QPushButton::clicked, dialog, [scroll, sizeToggle] {
            const bool fit = sizeToggle->property("fitMode").toBool();
            scroll->setFit(fit);
            sizeToggle->setProperty("fitMode", !fit);
            sizeToggle->setText(fit ? uiText("Actual size") : uiText("Fit"));
        });
        connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
        layout->addWidget(buttons);
        connect(dialog, &QDialog::finished, dialog, [dialog, scroll] {
            scroll->releaseImage();
            dialog->deleteLater();
        });
        connect(dialog, &QDialog::finished, this, [this] {
            m_imageDialog = nullptr;
#ifdef CLIPPER_NATIVE_MIXED_DEVELOPMENT
#include "../prototypes/native-mixed/native_detail_development_image_closed.inc"
#endif
        });
        m_imageDialog = dialog;
        dialog->show();
        scroll->updateImage();
        scroll->setFocus();
        return true;
    }

    bool copyImage(const QString &id)
    {
        QByteArray bytes;
        QImage image;
        QString mime;
        QString error;
        if (!readOwnedImage(id, &bytes, &image, &mime, &error)) {
            statusBar()->showMessage(error, 10000);
            return false;
        }
        auto *data = new QMimeData;
        data->setData(mime, bytes);
        data->setImageData(image);
        QApplication::clipboard()->setMimeData(data);
        const QMimeData *stored = QApplication::clipboard()->mimeData();
        if (!stored || stored->data(mime) != bytes) {
            statusBar()->showMessage(uiText("Image copy failed"), 10000);
            return false;
        }
        statusBar()->showMessage(uiText("Image copied"), 2500);
        return true;
    }

    void rebuildAttachmentActions()
    {
        while (QLayoutItem *item = m_attachmentLayout->takeAt(0)) {
            if (QWidget *widget = item->widget()) {
                widget->hide();
                widget->deleteLater();
            }
            delete item;
        }
        m_attachmentScroll->setVisible(!m_assets.isEmpty());
        for (const QJsonValue &value : m_assets) {
            const QJsonObject asset = value.toObject();
            const QString id = asset.value(QStringLiteral("id")).toString();
            auto *row = new QWidget(m_attachmentPanel);
            auto *layout = new QHBoxLayout(row);
            layout->setContentsMargins(0, 0, 0, 0);
            auto *name = new QLabel(asset.value(QStringLiteral("name")).toString(), row);
            name->setTextFormat(Qt::PlainText);
            layout->addWidget(name, 1);
            const QString imageError = inspectOwnedImage(id);
            if (!imageError.isEmpty()) {
                auto *error = new QLabel(imageError, row);
                error->setObjectName(QStringLiteral("attachmentError"));
                layout->addWidget(error);
            }
            auto *view = new QPushButton(uiText("View"), row);
            view->setObjectName(QStringLiteral("viewImageButton"));
            view->setProperty("assetId", id);
            auto *copy = new QPushButton(uiText("Copy image"), row);
            copy->setObjectName(QStringLiteral("copyImageButton"));
            copy->setProperty("assetId", id);
            connect(view, &QPushButton::clicked, this, [this, id] { showImage(id); });
            connect(copy, &QPushButton::clicked, this, [this, id] { copyImage(id); });
            layout->addWidget(view);
            layout->addWidget(copy);
            if (m_editing) {
                auto *remove = new QPushButton(uiText("Remove"), row);
                remove->setObjectName(QStringLiteral("removeImageButton"));
                remove->setProperty("assetId", id);
                connect(remove, &QPushButton::clicked, this,
                        [this, id] { removeImage(id); });
                layout->addWidget(remove);
            }
            m_attachmentLayout->addWidget(row);
        }
    }

    bool resolveUnsavedChanges(const char *actionSource)
    {
        if (!isDirty())
            return true;

        QMessageBox dialog(QMessageBox::Warning, {}, {},
                           QMessageBox::Save | QMessageBox::Discard | QMessageBox::Cancel, this);
        const auto retranslateDialog = [&dialog, actionSource] {
            dialog.setWindowTitle(uiText("Unsaved changes"));
            dialog.setText(uiText("Save changes before you %1?").arg(uiText(actionSource)));
            dialog.button(QMessageBox::Save)->setText(uiText("Save"));
            dialog.button(QMessageBox::Discard)->setText(uiText("Discard"));
            dialog.button(QMessageBox::Cancel)->setText(uiText("Cancel"));
        };
        connect(&m_language, &LanguageSettings::languageChanged, &dialog,
                [&dialog, retranslateDialog] {
                    retranslateDialog();
                    QTimer::singleShot(0, &dialog, retranslateDialog);
                });
        retranslateDialog();
        dialog.setDefaultButton(QMessageBox::Save);
        dialog.setEscapeButton(QMessageBox::Cancel);
        const int choice = dialog.exec();
        if (choice == QMessageBox::Save)
            return save();
        if (choice == QMessageBox::Discard) {
            cancelEditing();
            return true;
        }
        return false;
    }

    void retranslateUi()
    {
        setWindowTitle(uiText("Clipper — Detail"));
        m_copyButton->setText(uiText("Copy clip"));
        m_reloadButton->setText(uiText("Reload"));
        m_editButton->setText(uiText("Edit"));
        m_addFileButton->setText(uiText("Add image"));
        m_pasteImageButton->setText(uiText("Paste image"));
        m_cancelButton->setText(uiText("Cancel"));
        m_saveButton->setText(uiText("Save"));
        m_closeButton->setText(uiText("Close"));
        m_editor->setPlaceholderText(uiText("Edit Markdown"));
        m_titleLabel->setText(m_savedTitle.isEmpty() ? uiText("Untitled clip") : m_savedTitle);
        statusBar()->clearMessage();
        refreshPreview();
        rebuildAttachmentActions();
        updateActions();

        if (m_imageDialog) {
            m_imageDialog->setWindowTitle(uiText("Clipper — Image"));
            if (auto *size = m_imageDialog->findChild<QPushButton *>(
                    QStringLiteral("imageSizeToggleButton")))
                size->setText(size->property("fitMode").toBool()
                                  ? uiText("Fit") : uiText("Actual size"));
            if (auto *close = m_imageDialog->findChild<QPushButton *>(
                    QStringLiteral("closeImageButton")))
                close->setText(uiText("Close"));
        }
    }

    void applyTheme()
    {
        m_theme = currentTheme();
        const QColor background(m_theme.value(QStringLiteral("background")).toString());
        const QColor surface(m_theme.value(QStringLiteral("surface")).toString());
        const QColor foreground(m_theme.value(QStringLiteral("foreground")).toString());
        const QColor muted(m_theme.value(QStringLiteral("muted")).toString());
        const QColor accent(m_theme.value(QStringLiteral("accent")).toString());
        const QColor border(m_theme.value(QStringLiteral("border")).toString());

        QPalette themed = palette();
        themed.setColor(QPalette::Window, background);
        themed.setColor(QPalette::WindowText, foreground);
        themed.setColor(QPalette::Base, background);
        themed.setColor(QPalette::AlternateBase, surface);
        themed.setColor(QPalette::Text, foreground);
        themed.setColor(QPalette::Button, surface);
        themed.setColor(QPalette::ButtonText, foreground);
        themed.setColor(QPalette::Highlight, accent);
        themed.setColor(QPalette::HighlightedText, background);
        themed.setColor(QPalette::Mid, border);
        themed.setColor(QPalette::PlaceholderText, muted);
        setPalette(themed);

        QFont themedFont = font();
        themedFont.setFamily(m_theme.value(QStringLiteral("fontFamily")).toString());
        setFont(themedFont);
        setStyleSheet(QStringLiteral(
            "QMainWindow, QWidget { background: %1; color: %2; }"
            "QTextBrowser, QPlainTextEdit { background: %1; color: %2; border: 1px solid %3; padding: 10px; }"
            "QScrollArea#mixedReadingView { background: %1; border: 0; }"
            "QTextBrowser#mixedProse, QTextBrowser#mixedImages { background: transparent; border: 0; padding: 0; }"
            "QWidget#codeBlock { background: %4; border: 1px solid %3; border-radius: 6px; }"
            "QWidget#codeHeader { background: %4; border-bottom: 1px solid %3; }"
            "QTextEdit#codeText { background: %1; color: %2; border: 0; padding: 6px; }"
            "QWidget#codeLineNumbers { background: %1; }"
            "QLabel#codeLineNumber { background: transparent; color: %6; padding-right: 8px; }"
            "QPushButton { background: %4; color: %2; border: 1px solid %3; border-radius: 4px; padding: 7px 12px; }"
            "QPushButton:hover { border-color: %5; }"
            "QPushButton:focus { border: 2px solid %5; padding: 6px 11px; }"
            "QPushButton:disabled { color: %6; }"
            "QStatusBar { color: %6; border-top: 1px solid %3; }" )
                          .arg(background.name(), foreground.name(), border.name(), surface.name(),
                               accent.name(), muted.name()));
        QFont titleFont(QStringLiteral("serif"));
        titleFont.setStyleHint(QFont::Serif);
        titleFont.setPointSize(13);
        titleFont.setBold(true);
        m_titleLabel->setFont(titleFont);
    }

    static QJsonArray assetIds(const QJsonArray &assets)
    {
        QJsonArray ids;
        for (const QJsonValue &value : assets)
            ids.append(value.toObject().value(QStringLiteral("id")).toString());
        return ids;
    }

    void load()
    {
        const QJsonObject response = m_backend.dispatch(
            {{QStringLiteral("op"), QStringLiteral("clip.get")},
             {QStringLiteral("id"), m_clipId}});
        if (!response.value(QStringLiteral("ok")).toBool())
            throw std::runtime_error(responseError(response).toStdString());

        const QJsonObject clip = response.value(QStringLiteral("result")).toObject();
        if (clip.value(QStringLiteral("kind")).toString() != QStringLiteral("mixed"))
            throw std::runtime_error("This detail view only opens mixed clips; article clips are unsupported");

        m_loading = true;
        m_revision = clip.value(QStringLiteral("revision")).toInt();
        m_assets = clip.value(QStringLiteral("assets")).toArray();
        m_savedAssets = m_assets;
        m_savedText = clip.value(QStringLiteral("text")).toString();
        updateOwnedAssets();
        m_editor->setPlainText(m_savedText);
        m_savedTitle = clip.value(QStringLiteral("title")).toString().simplified();
        m_titleLabel->setText(m_savedTitle.isEmpty() ? uiText("Untitled clip") : m_savedTitle);
        m_loading = false;
        refreshPreview();
        setEditing(false);
        statusBar()->showMessage(uiText("Loaded revision %1").arg(m_revision), 3000);
    }

    bool save(QString *error = nullptr)
    {
        if (!isDirty()) {
            setEditing(false);
            return true;
        }
        const QJsonArray retainedAssetIds = assetIds(m_assets);
        const QJsonObject response = m_backend.dispatch(
            {{QStringLiteral("op"), QStringLiteral("clip.edit")},
             {QStringLiteral("id"), m_clipId},
             {QStringLiteral("text"), m_editor->toPlainText()},
             {QStringLiteral("assetIds"), retainedAssetIds},
             {QStringLiteral("expectedRevision"), m_revision}});
        if (!response.value(QStringLiteral("ok")).toBool()) {
            const QString message = responseError(response);
            if (error)
                *error = message;
            statusBar()->showMessage(uiText("Save failed: %1").arg(message), 10000);
            return false;
        }

        const QJsonObject clip = response.value(QStringLiteral("result")).toObject();
        m_loading = true;
        m_revision = clip.value(QStringLiteral("revision")).toInt();
        m_assets = clip.value(QStringLiteral("assets")).toArray();
        m_savedAssets = m_assets;
        m_savedText = clip.value(QStringLiteral("text")).toString();
        m_editor->setPlainText(m_savedText);
        m_savedTitle = clip.value(QStringLiteral("title")).toString().simplified();
        m_titleLabel->setText(m_savedTitle.isEmpty() ? uiText("Untitled clip") : m_savedTitle);
        m_preview->setOwnedAssets(m_assets);
        m_loading = false;
        refreshPreview();
        setEditing(false);
        statusBar()->showMessage(uiText("Saved revision %1").arg(m_revision), 5000);
        return true;
    }

    void refreshPreview()
    {
        if (m_loading)
            return;
        m_preview->setContent(m_savedText, m_assets, m_theme);
    }

    LanguageSettings m_language;
    RuntimeBackend m_backend;
    QString m_clipId;
    int m_revision = -1;
    QString m_savedText;
    QString m_savedTitle;
    QJsonArray m_assets;
    QJsonArray m_savedAssets;
    QJsonObject m_theme;
    QLabel *m_titleLabel = nullptr;
    QPlainTextEdit *m_editor = nullptr;
    MixedReadingView *m_preview = nullptr;
    QWidget *m_attachmentPanel = nullptr;
    QScrollArea *m_attachmentScroll = nullptr;
    QVBoxLayout *m_attachmentLayout = nullptr;
    QPushButton *m_copyButton = nullptr;
    QPushButton *m_reloadButton = nullptr;
    QPushButton *m_editButton = nullptr;
    QPushButton *m_addFileButton = nullptr;
    QPushButton *m_pasteImageButton = nullptr;
    QPushButton *m_cancelButton = nullptr;
    QPushButton *m_saveButton = nullptr;
    QPushButton *m_closeButton = nullptr;
    QTimer m_resizeRefresh;
    QTimer m_themeRefresh;
    bool m_loading = false;
    bool m_editing = false;
#ifdef CLIPPER_NATIVE_MIXED_DEVELOPMENT
#include "../prototypes/native-mixed/native_detail_development_fields.inc"
#endif
    QPointer<QDialog> m_imageDialog;
};

} // namespace

#ifndef CLIPPER_NATIVE_MIXED_NO_MAIN
#ifdef CLIPPER_NATIVE_MIXED_DEVELOPMENT
#include "../prototypes/native-mixed/native_detail_development_main.inc"
#else
int main(int argc, char **argv)
{
    for (int i = 1; i < argc; ++i) {
        if (QByteArray(argv[i]) == "--version") {
            std::puts(CLIPPER_VERSION);
            return 0;
        }
    }
    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("omarchy-clipper"));

    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Clipper mixed clip detail"));
    parser.addHelpOption();
    const QCommandLineOption dataDirOption(
        QStringLiteral("data-dir"), QStringLiteral("Collection directory (required)"),
        QStringLiteral("DIR"));
    const QCommandLineOption openOption(
        QStringLiteral("open"), QStringLiteral("Mixed clip ID (required)"), QStringLiteral("ID"));
    const QCommandLineOption backendOption(
        QStringLiteral("backend-bin"), QStringLiteral("Rust backend executable"),
        QStringLiteral("PATH"));
    const QCommandLineOption uiDirOption(
        QStringLiteral("ui-dir"), QStringLiteral("Ignored Reader UI directory"),
        QStringLiteral("DIR"));
    parser.addOptions({dataDirOption, openOption, backendOption, uiDirOption});
    parser.process(app);

    if (!parser.isSet(dataDirOption) || parser.value(dataDirOption).isEmpty()
        || !parser.isSet(openOption) || parser.value(openOption).isEmpty()) {
        std::fputs("--data-dir DIR and --open ID are required\n", stderr);
        return 2;
    }

    try {
        InstallationLifetimeLock lifetimeLock;
        NativeMixedWindow window(parser.value(dataDirOption), parser.value(openOption),
                                 parser.value(backendOption));
        window.show();
        return app.exec();
    } catch (const std::exception &error) {
        std::fprintf(stderr, "Clipper detail failed: %s\n", error.what());
        return 1;
    }
}
#endif
#endif
