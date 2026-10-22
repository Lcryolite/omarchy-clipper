CXX ?= g++
CLIPPER_VERSION ?= 0.1.0
QT_PACKAGES = Qt6Core Qt6Gui Qt6Qml Qt6Quick Qt6QuickControls2 Qt6WebEngineQuick libxml-2.0
CPPFLAGS += $(shell pkg-config --cflags $(QT_PACKAGES)) -DCLIPPER_VERSION='"$(CLIPPER_VERSION)"'
CXXFLAGS += -std=c++20 -O2 -Wall -Wextra -Wpedantic -fPIC '-ffile-prefix-map=$(CURDIR)=.'
LDLIBS += $(shell pkg-config --libs $(QT_PACKAGES))
MOC ?= $(shell qmake6 -query QT_HOST_LIBEXECS)/moc
RCC ?= $(shell qmake6 -query QT_HOST_LIBEXECS)/rcc
LRELEASE ?= lrelease6
TRANSLATION = build/translations/omarchy-clipper_zh.qm
VERSION_STAMP = build/.clipper-version
SOURCES = src/main.cpp src/application.cpp src/language.cpp src/theme.cpp src/html.cpp src/code_highlight.cpp src/runtime_backend.cpp
OBJECTS = $(patsubst src/%.cpp,build/%.o,$(SOURCES)) build/moc_application.o build/moc_language.o build/qrc_translations.o
HELPER_OBJECTS = build/helper_main.o build/html.o build/image.o build/desktop.o build/theme.o build/code_highlight.o
HELPER_LDLIBS = $(shell pkg-config --libs Qt6Core Qt6Gui libxml-2.0)
NATIVE_PACKAGES = Qt6Core Qt6Gui Qt6Widgets libxml-2.0
NATIVE_OBJECTS = build/native_detail.o build/language.o build/theme.o build/html.o build/code_highlight.o build/runtime_backend.o build/moc_language.o build/qrc_translations.o

.PHONY: all rust rust-check extension typecheck clean FORCE
all: build/clipper-reader build/clipper-helper build/clipper-native rust

build:
	mkdir -p build

FORCE:

$(VERSION_STAMP): FORCE | build
	@printf '%s\n' '$(CLIPPER_VERSION)' | cmp -s - $@ || printf '%s\n' '$(CLIPPER_VERSION)' > $@

build/moc_application.cpp: src/application.h Makefile | build
	$(MOC) $(CPPFLAGS) $< -o $@

build/moc_language.cpp: src/language.h Makefile | build
	$(MOC) $(CPPFLAGS) $< -o $@

$(TRANSLATION): translations/omarchy-clipper_zh.ts | build
	mkdir -p $(@D)
	$(LRELEASE) $< -qm $@

build/qrc_translations.cpp: translations/translations.qrc $(TRANSLATION) | build
	$(RCC) --name translations $< -o $@

build/%.o: src/%.cpp Makefile | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/main.o: $(VERSION_STAMP)

build/moc_application.o: build/moc_application.cpp Makefile
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/moc_language.o: build/moc_language.cpp Makefile
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/qrc_translations.o: build/qrc_translations.cpp Makefile
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/clipper-reader: $(OBJECTS)
	$(CXX) $^ $(LDLIBS) -o $@

build/helper_main.o: $(VERSION_STAMP)

build/clipper-helper: $(HELPER_OBJECTS)
	$(CXX) $^ $(HELPER_LDLIBS) -o $@

build/native_detail.o: src/native_detail.cpp src/language.h src/runtime_backend.h src/html.h src/code_highlight.h Makefile $(VERSION_STAMP) | build
	$(CXX) $(CPPFLAGS) $(shell pkg-config --cflags $(NATIVE_PACKAGES)) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/clipper-native: $(NATIVE_OBJECTS)
	$(CXX) $^ $(shell pkg-config --libs $(NATIVE_PACKAGES)) -o $@

rust:
	python3 scripts/build_rust_backend.py

rust-check:
	python3 scripts/build_rust_backend.py --check

extension:
	npm run build:extension

typecheck:
	npm run typecheck

clean:
	rm -rf build

-include $(OBJECTS:.o=.d) $(HELPER_OBJECTS:.o=.d) $(NATIVE_OBJECTS:.o=.d)
