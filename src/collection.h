#pragma once

#include <QJsonObject>
#include <QString>

#include <memory>

class Collection
{
public:
    explicit Collection(const QString &root);
    ~Collection();

    Collection(const Collection &) = delete;
    Collection &operator=(const Collection &) = delete;

    QJsonObject dispatch(const QJsonObject &request);
    QString assetPath(const QString &id) const;

private:
    class Impl;
    std::unique_ptr<Impl> d;
};
