import QtQuick
import Quickshell
import Quickshell.Wayland

PanelWindow {
  id: root
  objectName: "clipperHoverPreviewWindow"

  required property var panelWindow
  property alias controller: preview
  property alias requestFunction: preview.requestFunction
  property alias language: preview.language
  property alias previewEnabled: preview.previewEnabled
  property alias preferredSide: preview.preferredSide
  property alias backgroundColor: preview.backgroundColor
  property alias surfaceColor: preview.surfaceColor
  property alias foregroundColor: preview.foregroundColor
  property alias mutedColor: preview.mutedColor
  property alias accentColor: preview.accentColor
  property alias borderColor: preview.borderColor
  property alias fontFamily: preview.fontFamily

  readonly property string barPosition: panelWindow ? panelWindow.barPos : "top"
  readonly property real screenWidth: screen ? screen.width : 0
  readonly property real screenHeight: screen ? screen.height : 0
  readonly property rect usableGeometry: {
    var x = 0, y = 0, width = screenWidth, height = screenHeight
    if (!panelWindow) return Qt.rect(x, y, width, height)
    if (barPosition === "top") {
      y = panelWindow.barH
      height -= panelWindow.barH
    } else if (barPosition === "bottom") {
      height -= panelWindow.barH
    } else if (barPosition === "left") {
      x = panelWindow.barW
      width -= panelWindow.barW
    } else if (barPosition === "right") {
      width -= panelWindow.barW
    }
    return Qt.rect(x, y, Math.max(0, width), Math.max(0, height))
  }

  screen: panelWindow ? panelWindow.screen : null
  visible: preview.previewVisible
  color: "transparent"
  exclusionMode: ExclusionMode.Ignore

  WlrLayershell.namespace: "clipper-hover-preview"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

  anchors {
    top: true
    bottom: true
    left: true
    right: true
  }

  mask: Region {
    item: preview.previewVisible ? preview.surface : null
    Region { item: preview.previewVisible ? preview.transferCorridor : null }
  }

  HoverPreview {
    id: preview
    anchors.fill: parent
    clipperRect: root.panelWindow
      ? Qt.rect(root.panelWindow.cardOrigin.x, root.panelWindow.cardOrigin.y,
        root.panelWindow.contentWidth, root.panelWindow.contentHeight)
      : Qt.rect(0, 0, 0, 0)
    usableRect: root.usableGeometry
    screenOrigin: root.screen ? Qt.point(root.screen.x, root.screen.y) : Qt.point(0, 0)
  }
}
