import QtQuick
import QtQuick.Controls as Controls

Controls.Button {
  id: root
  property color foreground: "#eeeeee"
  property color fillColor: "transparent"
  property color hoverBackground: "#334455"
  property color borderColor: "#667788"
  property string fontFamily: "sans-serif"
  property int fontPixelSize: 13
  property string accessibleName: text

  implicitWidth: Math.max(72, label.implicitWidth + 24)
  implicitHeight: 34
  opacity: enabled ? 1 : 0.45
  activeFocusOnTab: enabled && visible
  hoverEnabled: true
  Accessible.name: accessibleName
  Keys.onReturnPressed: function(event) {
    if (root.enabled) root.clicked()
    event.accepted = true
  }
  Keys.onEnterPressed: function(event) {
    if (root.enabled) root.clicked()
    event.accepted = true
  }

  contentItem: PlainText {
    id: label
    text: root.text
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: root.fontPixelSize
    font.bold: true
    maximumLineCount: 1
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
  }

  background: Rectangle {
    radius: 5
    color: root.hovered && root.enabled ? root.hoverBackground : root.fillColor
    border.color: root.visualFocus ? root.foreground : root.borderColor
    border.width: root.visualFocus ? 2 : 1
  }

  HoverHandler {
    enabled: root.enabled
    cursorShape: Qt.PointingHandCursor
  }
}
