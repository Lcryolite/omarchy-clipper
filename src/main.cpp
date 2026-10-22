#include "application.h"
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonParseError>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QStandardPaths>
#include <QtWebEngineQuick>
#include <cstdio>
#include <memory>
#include <functional>
#include <cerrno>
#include <cstring>
#include <unistd.h>

#ifndef CLIPPER_VERSION
#define CLIPPER_VERSION "0.1.0"
#endif

namespace {
QString argument(const QStringList &args, const QString &key, const QString &fallback = {}) {
    const int index = args.indexOf(key);
    return index >= 0 && index + 1 < args.size() ? args[index + 1] : fallback;
}
QJsonObject decode(const QByteArray &bytes) {
    QJsonParseError error;
    const auto doc = QJsonDocument::fromJson(bytes, &error);
    if (error.error != QJsonParseError::NoError || !doc.isObject()) return {};
    return doc.object();
}
QJsonValue nativeMetadata(QJsonValue value) {
    if (value.isArray()) {
        QJsonArray result;
        for (const auto &entry : value.toArray()) result.append(nativeMetadata(entry));
        return result;
    }
    if (!value.isObject()) return value;
    auto object = value.toObject();
    if (object.contains("kind") && object.contains("id")) {
        object.remove("html"); object.remove("text"); object.remove("note");
    }
    if (object.contains("mime") && object.contains("id")) object.remove("url");
    for (auto it = object.begin(); it != object.end(); ++it) it.value() = nativeMetadata(it.value());
    return object;
}
QByteArray encode(const QJsonObject &value) { return QJsonDocument(value).toJson(QJsonDocument::Compact); }
QJsonObject failure(const QString &message) { return {{"ok", false}, {"error", message}}; }
bool readExact(char *out, size_t length) {
    size_t count = 0;
    while (count < length) {
        const size_t read = std::fread(out + count, 1, length - count, stdin);
        if (read == 0) return false;
        count += read;
    }
    return true;
}
int runNativeLoop(const std::function<QJsonObject(const QJsonObject &)> &dispatch) {
    quint32 length;
    while (readExact(reinterpret_cast<char *>(&length), sizeof length)) {
        if (length == 0 || length > 24 * 1024 * 1024) return 2;
        QByteArray input(length, '\0');
        if (!readExact(input.data(), length)) return 2;
        auto output = encode(nativeMetadata(dispatch(decode(input))).toObject());
        if (output.size() > 1024 * 1024)
            output = encode({{"ok", false}, {"error", "Native response is too large"}});
        length = output.size();
        if (std::fwrite(&length, sizeof length, 1, stdout) != 1
            || std::fwrite(output.data(), 1, output.size(), stdout) != size_t(output.size())) return 2;
        std::fflush(stdout);
    }
    return 0;
}
}

int main(int argc, char **argv) {
    QStringList args;
    for (int i = 0; i < argc; ++i) args << QString::fromLocal8Bit(argv[i]);
    if (args.contains("--help")) {
        std::puts("clipper --request | --open ID | --native --allowed-origin ORIGIN\nOptions: --data-dir DIR, --ui-dir DIR");
        return 0;
    }
    if (args.contains("--version")) {
        std::puts(CLIPPER_VERSION);
        return 0;
    }
    // Route public opens before initializing WebEngine. Article readers re-enter
    // with an internal flag; mixed details run in the standalone Widgets helper.
    if (args.contains("--open") && !args.contains("--web-reader")) {
        QCoreApplication qt(argc, argv);
        QCoreApplication::setApplicationName("omarchy-clipper");
        QCoreApplication::setOrganizationName("OmarchyClipper");
        const QString root = argument(args, "--data-dir", QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/omarchy-clipper");
        try {
            Application app(root);
            const auto response = app.dispatch({{"op", "clip.get"}, {"id", argument(args, "--open")}});
            if (!response.value("ok").toBool()) {
                std::fprintf(stderr, "%s\n", qPrintable(response.value("error").toString()));
                return 2;
            }
            const bool mixed = response.value("result").toObject().value("kind").toString() == "mixed";
            const QString program = mixed ? QCoreApplication::applicationDirPath() + "/clipper-native"
                                          : QCoreApplication::applicationFilePath();
            QStringList forwarded = args;
            forwarded[0] = program;
            if (mixed && !forwarded.contains("--data-dir"))
                forwarded << "--data-dir" << root;
            if (!mixed) forwarded << "--web-reader";
            QList<QByteArray> encoded;
            for (const auto &value : forwarded) encoded.append(value.toLocal8Bit());
            QList<char *> arguments;
            for (auto &value : encoded) arguments.append(value.data());
            arguments.append(nullptr);
            // Keep the caller's process lifetime, output and --ui-dir override.
            ::execv(QFile::encodeName(program).constData(), arguments.data());
            std::fprintf(stderr, "Could not open the detail window: %s\n", std::strerror(errno));
        } catch (const QString &error) { std::fprintf(stderr, "%s\n", qPrintable(error)); }
        catch (const std::exception &error) { std::fprintf(stderr, "%s\n", error.what()); }
        return 2;
    }
    const bool gui = args.contains("--open");
    const QByteArray platform = qgetenv("QT_QPA_PLATFORM");
    const bool wayland = platform.startsWith("wayland")
        || (platform.isEmpty() && !qEnvironmentVariableIsEmpty("WAYLAND_DISPLAY"));
    // Qt WebEngine can lose its threaded scene-graph surface when Wayland
    // hides a workspace. Keep the Reader rendering on the GUI thread instead.
    if (gui && wayland && !qEnvironmentVariableIsSet("QSG_RENDER_LOOP"))
        qputenv("QSG_RENDER_LOOP", "basic");
    if (gui) { registerReaderScheme(); QtWebEngineQuick::initialize(); }
    std::unique_ptr<QCoreApplication> qt;
    if (gui) qt = std::make_unique<QGuiApplication>(argc, argv);
    else qt = std::make_unique<QCoreApplication>(argc, argv);
    QCoreApplication::setApplicationName("omarchy-clipper");
    QCoreApplication::setOrganizationName("OmarchyClipper");
    const QString root = argument(args, "--data-dir", QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/omarchy-clipper");
    if (args.contains("--native")) {
        const QString expected = argument(args, "--allowed-origin");
        const int originArg = args.indexOf("--allowed-origin") + 2;
        if (expected.isEmpty() || originArg >= args.size() || args[originArg] != expected || !expected.startsWith("chrome-extension://")) {
            std::fputs("Browser origin is not allowed\n", stderr); return 2;
        }
        try {
            Application app(root, argument(args, "--open"));
            return runNativeLoop([&app](const QJsonObject &request) {
                return app.dispatch(request, true);
            });
        } catch (const QString &error) {
            return runNativeLoop([error](const QJsonObject &request) {
                return Application::nativeOperationAllowed(request)
                    ? failure(error)
                    : failure("Operation is not available to browser clients");
            });
        } catch (const std::exception &error) {
            const QString message = QString::fromLocal8Bit(error.what());
            return runNativeLoop([message](const QJsonObject &request) {
                return Application::nativeOperationAllowed(request)
                    ? failure(message)
                    : failure("Operation is not available to browser clients");
            });
        }
    }
    try {
        Application app(root, argument(args, "--open"));
        if (args.contains("--request")) {
            QFile input;
            if (!input.open(stdin, QIODevice::ReadOnly)) return 2;
            const auto bytes = input.read(24 * 1024 * 1024 + 1);
            const auto response = bytes.size() > 24 * 1024 * 1024 ? QJsonObject{{"ok", false}, {"error", "Request is too large"}} : app.dispatch(decode(bytes));
            const auto output = encode(response) + '\n';
            std::fwrite(output.data(), 1, output.size(), stdout);
            return 0;
        }
        if (!gui) { std::fputs("Use --request or --open ID\n", stderr); return 2; }
        configureReader(app);
        QQmlApplicationEngine engine;
        QObject::connect(&app, &Application::languageChanged, &engine,
                         [&engine] { engine.retranslate(); });
        engine.rootContext()->setContextProperty("bridge", &app);
        QString ui = argument(args, "--ui-dir");
        if (ui.isEmpty()) {
            const QDir applicationDir(QCoreApplication::applicationDirPath());
            ui = applicationDir.absoluteFilePath("../share/omarchy-clipper/ui");
            if (!QFile::exists(ui + "/Reader.qml"))
                ui = applicationDir.absoluteFilePath("../ui");
        }
        engine.load(QUrl::fromLocalFile(ui + "/Reader.qml"));
        if (engine.rootObjects().isEmpty()) return 2;
        return qt->exec();
    } catch (const QString &error) { std::fprintf(stderr, "%s\n", qPrintable(error)); }
    catch (const std::exception &error) { std::fprintf(stderr, "%s\n", error.what()); }
    return 2;
}
