import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Rectangle {
  id: root
  objectName: "clipperSettingsPane"

  property var settings: ({})
  property var persistFunction: null
  property var previewEnabledOverride
  property var previewSideOverride
  property var languageOverride
  property string resolvedLanguage: "en"
  ClipperTranslations { id: translations; language: root.resolvedLanguage }

  readonly property bool previewEnabled: previewEnabledOverride !== undefined
    ? previewEnabledOverride === true
    : settings && settings.previewEnabled === true
  readonly property string previewSide: previewSideOverride !== undefined
    ? String(previewSideOverride)
    : (settings && settings.previewSide === "left" ? "left" : "right")
  readonly property string language: languageOverride !== undefined
    ? String(languageOverride)
    : (settings && (settings.language === "zh" || settings.language === "en")
      ? String(settings.language) : "system")

  signal closeRequested()

  function focusReturnButton() { returnButton.forceActiveFocus() }

  function setPreference(key, value) {
    if (key === "previewEnabled") previewEnabledOverride = value === true
    else if (key === "previewSide") previewSideOverride = value === "left" ? "left" : "right"
    else if (key === "language") languageOverride = value === "zh" || value === "en" ? value : "system"
    if (persistFunction) persistFunction(key, value)
  }

  function reconcileOverrides() {
    if (previewEnabledOverride !== undefined && settings
        && settings.previewEnabled !== undefined
        && (settings.previewEnabled === true) === previewEnabledOverride)
      previewEnabledOverride = undefined
    if (previewSideOverride !== undefined && settings
        && settings.previewSide !== undefined
        && String(settings.previewSide) === previewSideOverride)
      previewSideOverride = undefined
    if (languageOverride !== undefined && settings
        && settings.language !== undefined
        && String(settings.language) === languageOverride)
      languageOverride = undefined
  }

  onSettingsChanged: reconcileOverrides()

  color: "transparent"
  implicitHeight: settingsLayout.implicitHeight

  ColumnLayout {
    id: settingsLayout
    anchors.fill: parent
    spacing: Style.spacing.panelGap

    RowLayout {
      Layout.fillWidth: true
      PlainText {
        text: translations.text("Settings")
        color: root.foregroundColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
        font.bold: true
        Layout.fillWidth: true
      }
      PanelActionButton {
        id: returnButton
        objectName: "settingsCloseButton"
        iconText: "󰁍"
        tooltipText: translations.text("Back to collection")
        Accessible.name: tooltipText
        Accessible.role: Accessible.Button
        foreground: root.foregroundColor
        fontFamily: root.fontFamily
        focusable: true
        onClicked: root.closeRequested()
      }
    }

    Toggle {
      objectName: "previewEnabledSwitch"
      Layout.fillWidth: true
      label: translations.text("Hover preview")
      description: translations.text("Show saved content beside Clipper while hovering an entry.")
      foreground: root.foregroundColor
      fontFamily: root.fontFamily
      checked: root.previewEnabled
      Accessible.name: label
      Accessible.role: Accessible.CheckBox
      Accessible.checkable: true
      Accessible.checked: checked
      onClicked: root.setPreference("previewEnabled", !checked)
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.rowGap
      enabled: root.previewEnabled
      opacity: enabled ? 1 : 0.4
      PanelSectionHeader {
        text: translations.text("Preferred preview side")
        foreground: root.foregroundColor
        fontFamily: root.fontFamily
      }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.controlGap
        Button {
          id: leftButton
          Layout.fillHeight: true
          objectName: "previewSideLeft"
          text: translations.text("Left")
          active: root.previewSide === "left"
          bordered: true
          focusable: true
          foreground: root.foregroundColor
          fontFamily: root.fontFamily
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          onClicked: root.setPreference("previewSide", "left")
        }
        Button {
          id: rightButton
          Layout.fillHeight: true
          objectName: "previewSideRight"
          text: translations.text("Right")
          active: root.previewSide === "right"
          bordered: true
          focusable: true
          foreground: root.foregroundColor
          fontFamily: root.fontFamily
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          onClicked: root.setPreference("previewSide", "right")
        }
      }
    }

    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.rowGap
      PanelSectionHeader {
        text: translations.text("Interface language")
        foreground: root.foregroundColor
        fontFamily: root.fontFamily
      }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.spacing.controlGap
        Button {
          id: systemButton
          Layout.fillHeight: true
          objectName: "languageSystem"
          text: translations.text("Follow system")
          active: root.language === "system"
          bordered: true
          focusable: true
          foreground: root.foregroundColor
          fontFamily: root.fontFamily
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          onClicked: root.setPreference("language", "system")
        }
        Button {
          id: chineseButton
          Layout.fillHeight: true
          objectName: "languageChinese"
          text: "中文"
          active: root.language === "zh"
          bordered: true
          focusable: true
          foreground: root.foregroundColor
          fontFamily: root.fontFamily
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          onClicked: root.setPreference("language", "zh")
        }
        Button {
          id: englishButton
          Layout.fillHeight: true
          objectName: "languageEnglish"
          text: "English"
          active: root.language === "en"
          bordered: true
          focusable: true
          foreground: root.foregroundColor
          fontFamily: root.fontFamily
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          onClicked: root.setPreference("language", "en")
        }
      }
    }

    Item { Layout.fillHeight: true }
  }

  property color foregroundColor: Commons.Color.foreground
  property color mutedColor: Qt.darker(foregroundColor, 1.5)
  property string fontFamily: Style.font.family
}
