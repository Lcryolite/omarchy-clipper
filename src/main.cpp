#include "application.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QStandardPaths>
#include <QtWebEngineQuick>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <memory>
#include <unistd.h>

#ifndef CLIPPER_VERSION
#define CLIPPER_VERSION "0.1.0"
#endif

namespace {

QString argument(const QStringList &args, const QString &key, const QString &fallback = {})
{
    const int index = args.indexOf(key);
    return index >= 0 && index + 1 < args.size() ? args[index + 1] : fallback;
}

QJsonObject decode(const QByteArray &bytes)
{
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(bytes, &error);
    return error.error == QJsonParseError::NoError && document.isObject()
        ? document.object() : QJsonObject();
}

QByteArray encode(const QJsonObject &value)
{
    return QJsonDocument(value).toJson(QJsonDocument::Compact);
}

QString dataDirectory(const QStringList &args)
{
    return argument(
        args, QStringLiteral("--data-dir"),
        QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)
            + QStringLiteral("/omarchy-clipper"));
}

int runForwardedRequest(const QStringList &args)
{
    QFile input;
    if (!input.open(stdin, QIODevice::ReadOnly))
        return 2;
    const QByteArray bytes = input.read(24 * 1024 * 1024 + 1);
    QJsonObject response;
    try {
        if (bytes.size() > 24 * 1024 * 1024) {
            response = {{QStringLiteral("ok"), false},
                        {QStringLiteral("error"), QStringLiteral("Request is too large")}};
        } else {
            RuntimeBackend backend(
                dataDirectory(args), argument(args, QStringLiteral("--backend-bin")));
            response = backend.dispatch(decode(bytes));
        }
    } catch (const std::exception &error) {
        std::fprintf(stderr, "%s\n", error.what());
        return 2;
    }
    const QByteArray output = encode(response) + '\n';
    std::fwrite(output.data(), 1, output.size(), stdout);
    return 0;
}

int routeOpen(int argc, char **argv, const QStringList &args)
{
    QCoreApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("omarchy-clipper"));
    QCoreApplication::setOrganizationName(QStringLiteral("OmarchyClipper"));
    try {
        InstallationLifetimeLock lifetimeLock;
        const QString root = dataDirectory(args);
        Application bridge(root, {}, nullptr, argument(args, QStringLiteral("--backend-bin")));
        const QJsonObject response = bridge.dispatch(
            {{QStringLiteral("op"), QStringLiteral("clip.get")},
             {QStringLiteral("id"), argument(args, QStringLiteral("--open"))}});
        if (!response.value(QStringLiteral("ok")).toBool()) {
            std::fprintf(stderr, "%s\n",
                         qPrintable(response.value(QStringLiteral("error")).toString()));
            return 2;
        }
        const bool mixed = response.value(QStringLiteral("result")).toObject()
                               .value(QStringLiteral("kind")).toString()
            == QStringLiteral("mixed");
        const QString program = mixed ? nativeDetailExecutable()
                                      : QCoreApplication::applicationFilePath();
        QStringList forwarded = args;
        forwarded[0] = program;
        if (mixed && !forwarded.contains(QStringLiteral("--data-dir")))
            forwarded << QStringLiteral("--data-dir") << root;
        if (!mixed)
            forwarded << QStringLiteral("--web-reader");
        QList<QByteArray> encodedArguments;
        for (const QString &value : forwarded)
            encodedArguments.append(value.toLocal8Bit());
        QList<char *> arguments;
        for (QByteArray &value : encodedArguments)
            arguments.append(value.data());
        arguments.append(nullptr);
        ::execv(QFile::encodeName(program).constData(), arguments.data());
        std::fprintf(stderr, "Could not open the detail window: %s\n", std::strerror(errno));
    } catch (const QString &error) {
        std::fprintf(stderr, "%s\n", qPrintable(error));
    } catch (const std::exception &error) {
        std::fprintf(stderr, "%s\n", error.what());
    }
    return 2;
}

} // namespace

int main(int argc, char **argv)
{
    QStringList args;
    for (int index = 0; index < argc; ++index)
        args << QString::fromLocal8Bit(argv[index]);
    if (args.contains(QStringLiteral("--help"))) {
        std::puts("clipper-reader --open ID [--data-dir DIR] [--ui-dir DIR] "
                  "[--backend-bin PATH]");
        return 0;
    }
    if (args.contains(QStringLiteral("--version"))) {
        std::puts(CLIPPER_VERSION);
        return 0;
    }
    if (args.contains(QStringLiteral("--native"))) {
        std::fputs("Browser native messaging is provided by the Rust backend\n", stderr);
        return 2;
    }
    if (args.contains(QStringLiteral("--open"))
        && !args.contains(QStringLiteral("--web-reader"))) {
        return routeOpen(argc, argv, args);
    }

    const bool request = args.contains(QStringLiteral("--request"));
    const bool gui = args.contains(QStringLiteral("--open"));
    if (!request && !gui) {
        std::fputs("Use --open ID\n", stderr);
        return 2;
    }
    const QByteArray platform = qgetenv("QT_QPA_PLATFORM");
    const bool wayland = platform.startsWith("wayland")
        || (platform.isEmpty() && !qEnvironmentVariableIsEmpty("WAYLAND_DISPLAY"));
    if (gui && wayland && !qEnvironmentVariableIsSet("QSG_RENDER_LOOP"))
        qputenv("QSG_RENDER_LOOP", "basic");
    if (gui) {
        registerReaderScheme();
        QtWebEngineQuick::initialize();
    }
    std::unique_ptr<QCoreApplication> application;
    if (gui)
        application = std::make_unique<QGuiApplication>(argc, argv);
    else
        application = std::make_unique<QCoreApplication>(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("omarchy-clipper"));
    QCoreApplication::setOrganizationName(QStringLiteral("OmarchyClipper"));
    try {
        InstallationLifetimeLock lifetimeLock;
        if (request)
            return runForwardedRequest(args);
        Application bridge(dataDirectory(args), argument(args, QStringLiteral("--open")),
                           nullptr, argument(args, QStringLiteral("--backend-bin")));
        configureReader(bridge);
        QQmlApplicationEngine engine;
        QObject::connect(&bridge, &Application::languageChanged, &engine,
                         [&engine] { engine.retranslate(); });
        engine.rootContext()->setContextProperty(QStringLiteral("bridge"), &bridge);
        QString ui = argument(args, QStringLiteral("--ui-dir"));
        if (ui.isEmpty()) {
            const QDir applicationDir(QCoreApplication::applicationDirPath());
            ui = applicationDir.absoluteFilePath(
                QStringLiteral("../../share/omarchy-clipper/ui"));
            if (!QFile::exists(ui + QStringLiteral("/Reader.qml")))
                ui = applicationDir.absoluteFilePath(QStringLiteral("../ui"));
        }
        engine.load(QUrl::fromLocalFile(ui + QStringLiteral("/Reader.qml")));
        if (engine.rootObjects().isEmpty())
            return 2;
        return application->exec();
    } catch (const QString &error) {
        std::fprintf(stderr, "%s\n", qPrintable(error));
    } catch (const std::exception &error) {
        std::fprintf(stderr, "%s\n", error.what());
    }
    return 2;
}
