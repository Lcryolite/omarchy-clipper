import QtQuick
import QtQuick.Layouts

Rectangle {
  id: card
  objectName: "clipCard"

  required property var modelData
  property var previewController: null
  property string language: "en"
  ClipperTranslations { id: translations; language: card.language }
  property bool trashMode: false
  property color surfaceColor: "#302d29"
  property color foregroundColor: "#f0e8da"
  property color mutedColor: "#b9aa96"
  property color accentColor: "#d6a86e"
  property color borderColor: "#5b5146"
  property string fontFamily: "sans-serif"
  property int cornerRadius: 6
  property int contentMargin: 8
  property int contentSpacing: 9
  property int coverWidth: 62
  property int actionSize: 28
  property int titleSize: 14
  property int captionSize: 11
  property int bodySize: 12
  property int iconSize: 20
  property string stateLabel: ""
  property bool needsAttention: false

  readonly property bool hasCover: !!(modelData && modelData.assets && modelData.assets.length > 0
    && modelData.assets[0] && modelData.assets[0].url)
  readonly property alias coverItem: cover
  readonly property alias coverHitRegion: coverMouse
  readonly property alias textHitRegion: titleMouse

  signal openRequested(string clipId)
  signal lifecycleRequested(string clipId)

  radius: cornerRadius
  color: cardHover.hovered ? Qt.lighter(surfaceColor, 1.12) : surfaceColor
  border.color: activeFocus ? accentColor : borderColor
  border.width: activeFocus ? 2 : 1
  activeFocusOnTab: true

  Keys.onPressed: function(event) {
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
      card.openRequested(String(modelData.id))
      event.accepted = true
    }
  }

  HoverHandler {
    id: cardHover
    onHoveredChanged: {
      if (!card.previewController) return
      if (hovered) card.previewController.arm(String(card.modelData.id), card)
      else card.previewController.scheduleClose(String(card.modelData.id))
    }
  }

  RowLayout {
    anchors.fill: parent
    anchors.margins: card.contentMargin
    spacing: card.contentSpacing

    Rectangle {
      id: cover
      objectName: "clipCover"
      Layout.preferredWidth: card.coverWidth
      Layout.fillHeight: true
      radius: Math.max(2, card.cornerRadius - 1)
      color: Qt.rgba(card.foregroundColor.r, card.foregroundColor.g, card.foregroundColor.b, 0.08)
      clip: true

      Image {
        anchors.fill: parent
        source: card.hasCover ? String(card.modelData.assets[0].url) : ""
        sourceSize.width: Math.min(512, Math.max(1, Math.ceil(cover.width
          * (card.window && card.window.screen ? card.window.screen.devicePixelRatio : 1))))
        sourceSize.height: Math.min(512, Math.max(1, Math.ceil(cover.height
          * (card.window && card.window.screen ? card.window.screen.devicePixelRatio : 1))))
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        visible: status === Image.Ready
      }
      PlainText {
        anchors.centerIn: parent
        text: card.modelData.kind === "article" ? "󰈙" : (card.hasCover ? "󰋩" : "󰈔")
        color: card.mutedColor
        font.family: card.fontFamily
        font.pixelSize: card.iconSize
      }

      MouseArea {
        id: coverMouse
        objectName: "clipCoverHitRegion"
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: card.openRequested(String(card.modelData.id))
      }
    }

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true

      ColumnLayout {
        anchors.fill: parent
        spacing: 1
        PlainText {
          objectName: "clipTitle"
          Layout.fillWidth: true
          text: String(card.modelData.title || card.modelData.text
            || translations.text(card.modelData.kind === "article" ? "Untitled article" : "Untitled clip")).split("\n")[0]
          color: card.foregroundColor
          font.family: card.fontFamily
          font.pixelSize: card.titleSize
          font.bold: true
          maximumLineCount: 1
        }
        PlainText {
          Layout.fillWidth: true
          text: card.stateLabel
          color: card.needsAttention ? card.accentColor : card.mutedColor
          font.family: card.fontFamily
          font.pixelSize: card.captionSize
          maximumLineCount: 1
        }
        PlainText {
          Layout.fillWidth: true
          text: String(card.modelData.text || card.modelData.scope || "")
          color: card.mutedColor
          font.family: card.fontFamily
          font.pixelSize: card.bodySize
          maximumLineCount: 1
        }
      }

      MouseArea {
        id: titleMouse
        objectName: "clipTextHitRegion"
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: card.openRequested(String(card.modelData.id))
      }
    }

    Rectangle {
      width: card.actionSize
      height: card.actionSize
      radius: card.cornerRadius
      color: actionMouse.containsMouse
        ? Qt.rgba(card.foregroundColor.r, card.foregroundColor.g, card.foregroundColor.b, 0.12)
        : "transparent"
      PlainText {
        anchors.centerIn: parent
        text: card.trashMode ? "󰑓" : "󰆴"
        color: card.mutedColor
        font.family: card.fontFamily
        font.pixelSize: card.iconSize
      }
      MouseArea {
        id: actionMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: function(mouse) {
          mouse.accepted = true
          card.lifecycleRequested(String(card.modelData.id))
        }
      }
    }
  }
}
