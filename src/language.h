#pragma once

#include <QFileSystemWatcher>
#include <QObject>
#include <QTimer>
#include <QTranslator>

QString localizedUserMessage(const QString &message);

class LanguageSettings final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString language READ language NOTIFY languageChanged)
    Q_PROPERTY(QString resolvedLanguage READ resolvedLanguage NOTIFY languageChanged)

public:
    explicit LanguageSettings(QObject *parent = nullptr);

    QString language() const { return m_language; }
    QString resolvedLanguage() const { return m_resolvedLanguage; }
    QString settingsPath() const { return m_settingsPath; }
    QString error() const { return m_error; }

signals:
    void languageChanged();

private:
    void refresh();
    void watchSettings();
    void installTranslation(const QString &language);

    QString m_settingsPath;
    QString m_error;
    QString m_language = QStringLiteral("system");
    QString m_resolvedLanguage = QStringLiteral("en");
    QFileSystemWatcher m_watcher;
    QTimer m_refreshTimer;
    QTranslator m_translator;
};
