#pragma once

#include <QString>

namespace clipper {

struct CodeHighlightResult {
    QString html;
    QString displayLanguage;
    bool knownLanguage = false;
    bool syntaxHighlighted = false;
};

// Returns an HTML-safe fragment without pre/code wrappers. Known languages use
// GNU Source-highlight's stable keyword, type, string, comment, number, and
// symbol classes when its optional executable is available. The source is
// never normalized; callers must retain it for exact copying.
CodeHighlightResult highlightCode(const QString &source, const QString &languageHint,
                                  bool allowExternalHighlighter = true);

} // namespace clipper
