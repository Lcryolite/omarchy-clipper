#pragma once

#include <QString>

#include <stdexcept>
#include <utility>

class ValidationError final : public std::runtime_error
{
public:
    explicit ValidationError(const QString &message, QString errorCode = {})
        : std::runtime_error(message.toStdString())
        , code(std::move(errorCode))
    {
    }

    const QString code;
};
