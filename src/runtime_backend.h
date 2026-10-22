#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QString>

class InstallationLifetimeLock
{
public:
    InstallationLifetimeLock();
    ~InstallationLifetimeLock();

    InstallationLifetimeLock(const InstallationLifetimeLock &) = delete;
    InstallationLifetimeLock &operator=(const InstallationLifetimeLock &) = delete;

private:
    int m_descriptor = -1;
};

class RuntimeBackend
{
public:
    explicit RuntimeBackend(QString root, QString backend = {});

    RuntimeBackend(const RuntimeBackend &) = delete;
    RuntimeBackend &operator=(const RuntimeBackend &) = delete;

    QJsonObject dispatch(const QJsonObject &request);
    QByteArray readAsset(const QString &id, QString *mime = nullptr,
                         QString *error = nullptr) const;
    QString backendPath() const { return m_backend; }

private:
    QString m_root;
    QString m_backend;
    QString m_helper;
};
