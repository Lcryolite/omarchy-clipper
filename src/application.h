#pragma once

#include <QObject>
#include <QJsonObject>
#include <QVariantMap>
#include "collection.h"
#include "language.h"

class Application : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString initialClipId READ initialClipId CONSTANT)
    Q_PROPERTY(QString dataDir READ dataDir CONSTANT)
    Q_PROPERTY(QString language READ language NOTIFY languageChanged)
    Q_PROPERTY(QString resolvedLanguage READ resolvedLanguage NOTIFY languageChanged)
public:
    Application(QString root, QString clipId = {}, QObject *parent = nullptr);
    Q_INVOKABLE QVariantMap request(const QVariantMap &value);
    Q_INVOKABLE QString readerUrl(const QString &id) const;
    Q_INVOKABLE QString localizedMessage(const QString &message) const
        { return localizedUserMessage(message); }
    QJsonObject dispatch(const QJsonObject &value, bool native = false);
    static bool nativeOperationAllowed(const QJsonObject &value);
    QString initialClipId() const { return m_clipId; }
    QString dataDir() const { return m_root; }
    QString language() const { return m_language.language(); }
    QString resolvedLanguage() const { return m_language.resolvedLanguage(); }
    Collection &collection() { return m_collection; }
signals:
    void languageChanged();
private:
    QString m_root;
    QString m_clipId;
    LanguageSettings m_language;
    Collection m_collection;
};

void registerReaderScheme();
void configureReader(Application &application);
