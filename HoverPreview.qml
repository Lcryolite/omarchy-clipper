import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts

Item {
  id: root
  objectName: "hoverPreviewLayer"
  visible: shown

  property var requestFunction: null
  property string language: "en"
  ClipperTranslations { id: translations; language: root.language }
  property bool previewEnabled: false
  property string preferredSide: "right"
  property rect clipperRect: Qt.rect(0, 0, 0, 0)
  property rect usableRect: Qt.rect(0, 0, width, height)
  property point screenOrigin: Qt.point(0, 0)
  property int normalPreviewWidth: 420
  property int normalPreviewHeight: 560
  property int minimumPreviewWidth: 260
  property int minimumPreviewHeight: 280
  property int placementGap: 12
  property int placementMargin: 12
  // Hover previews show at most 600 Unicode characters from saved text.
  readonly property int excerptCharacterLimit: 600
  property color backgroundColor: "#252320"
  property color surfaceColor: "#302d29"
  property color foregroundColor: "#f0e8da"
  property color mutedColor: "#b9aa96"
  property color accentColor: "#d6a86e"
  property color borderColor: "#5b5146"
  property string fontFamily: "sans-serif"

  property var previewClip: null
  property string armedClipId: ""
  property Item armedAnchor: null
  property int generation: 0
  property bool shown: false
  property bool requestPending: false
  property string placementSide: ""

  readonly property alias surface: previewCard
  readonly property alias transferCorridor: corridor
  readonly property bool previewVisible: shown
  readonly property real previewX: previewCard.x
  readonly property real previewY: previewCard.y
  readonly property real previewWidth: previewCard.width
  readonly property real previewHeight: previewCard.height
  readonly property int assetCount: previewClip && Array.isArray(previewClip.assets) ? previewClip.assets.length : 0
  readonly property string bodyText: previewBody()
  readonly property real scrollMaximum: Math.max(0, previewScroll.contentHeight - previewScroll.height)
  readonly property real scrollPosition: previewScroll.contentY

  function boundedExcerpt(source) {
    var value = String(source || "")
    var offset = 0
    var characterCount = 0
    while (offset < value.length && characterCount < excerptCharacterLimit) {
      var first = value.charCodeAt(offset)
      if (first >= 0xd800 && first <= 0xdbff && offset + 1 < value.length) {
        var second = value.charCodeAt(offset + 1)
        if (second >= 0xdc00 && second <= 0xdfff) offset++
      }
      offset++
      characterCount++
    }
    return offset < value.length ? value.substring(0, offset) + "…" : value
  }

  function unicodeCharacter(codePoint) {
    if (codePoint <= 0xffff) return String.fromCharCode(codePoint)
    var adjusted = codePoint - 0x10000
    return String.fromCharCode(0xd800 + (adjusted >> 10), 0xdc00 + (adjusted & 0x3ff))
  }

  function decodeHtmlEntities(source) {
    var named = ({ amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'", nbsp: " " })
    return source.replace(/&(#(?:x[0-9a-f]+|[0-9]+)|amp|lt|gt|quot|apos|nbsp);/gi,
      function(match, entity) {
        var normalized = String(entity).toLowerCase()
        if (normalized.charAt(0) !== "#") return named[normalized]
        var hexadecimal = normalized.charAt(1) === "x"
        var codePoint = parseInt(normalized.substring(hexadecimal ? 2 : 1), hexadecimal ? 16 : 10)
        if (!isFinite(codePoint) || codePoint < 0 || codePoint > 0x10ffff
            || (codePoint >= 0xd800 && codePoint <= 0xdfff)) return match
        return unicodeCharacter(codePoint)
      })
  }

  function readableHtmlText(source) {
    var value = String(source || "")
    value = value.replace(/<\s*br\b[^>]*>/gi, "\n")
    value = value.replace(/<\s*\/\s*(?:p|div|h[1-6]|li|blockquote|pre|tr|section|article)\s*>/gi, "\n")
    value = value.replace(/<\s*li\b[^>]*>/gi, "• ")
    value = value.replace(/<[^>]*>/g, "")
    value = decodeHtmlEntities(value)
    return value.replace(/[ \t]+\n/g, "\n").replace(/\n[ \t]+/g, "\n")
      .replace(/\n{3,}/g, "\n\n").trim()
  }

  function previewBody() {
    if (!previewClip) return ""
    if (previewClip.kind !== "article") return boundedExcerpt(previewClip.text)
    var readableText = String(previewClip.text || "")
    return boundedExcerpt(readableText.trim().length > 0 ? readableText : readableHtmlText(previewClip.html))
  }

  function arm(clipId, anchor) {
    if (!previewEnabled || !clipId || !anchor) return
    generation++
    armedClipId = String(clipId)
    armedAnchor = anchor
    requestPending = false
    closeTimer.stop()
    openTimer.restart()
  }

  function cancelArm() {
    if (!openTimer.running) return
    openTimer.stop()
    generation++
    armedClipId = ""
    armedAnchor = null
  }

  function keepOpen() {
    closeTimer.stop()
  }

  function scheduleClose(leavingClipId) {
    if (openTimer.running) {
      if (!leavingClipId || armedClipId === String(leavingClipId)) cancelArm()
      return
    }
    if (leavingClipId && armedClipId && armedClipId !== String(leavingClipId)) return
    if (shown || previewClip || requestPending) closeTimer.restart()
  }

  function dismissNow() {
    generation++
    openTimer.stop()
    closeTimer.stop()
    shown = false
    requestPending = false
    previewClip = null
    armedClipId = ""
    armedAnchor = null
    placementSide = ""
    previewScroll.contentY = 0
  }

  function anchorPoint() {
    if (!armedAnchor) return Qt.point(clipperRect.x, clipperRect.y)
    if (armedAnchor.mapToGlobal) {
      var global = armedAnchor.mapToGlobal(0, 0)
      return Qt.point(global.x - screenOrigin.x, global.y - screenOrigin.y)
    }
    return armedAnchor.mapToItem(root, 0, 0)
  }

  function placePreview() {
    if (!armedAnchor || clipperRect.width <= 0 || clipperRect.height <= 0
        || usableRect.width <= 0 || usableRect.height <= 0) return false

    var innerLeft = usableRect.x + placementMargin
    var innerRight = usableRect.x + usableRect.width - placementMargin
    var innerTop = usableRect.y + placementMargin
    var innerBottom = usableRect.y + usableRect.height - placementMargin
    var height = Math.min(normalPreviewHeight, innerBottom - innerTop)
    if (height < minimumPreviewHeight) return false

    var rooms = ({
      left: clipperRect.x - placementGap - innerLeft,
      right: innerRight - (clipperRect.x + clipperRect.width + placementGap)
    })
    var preferred = preferredSide === "left" ? "left" : "right"
    var opposite = preferred === "left" ? "right" : "left"
    var side = ""
    var width = normalPreviewWidth
    if (rooms[preferred] >= normalPreviewWidth) side = preferred
    else if (rooms[opposite] >= normalPreviewWidth) side = opposite
    else if (rooms[preferred] >= minimumPreviewWidth) {
      side = preferred
      width = rooms[preferred]
    } else if (rooms[opposite] >= minimumPreviewWidth) {
      side = opposite
      width = rooms[opposite]
    } else return false

    placementSide = side
    previewCard.width = Math.round(Math.min(normalPreviewWidth, width))
    previewCard.height = Math.round(height)
    previewCard.x = Math.round(side === "left"
      ? clipperRect.x - placementGap - previewCard.width
      : clipperRect.x + clipperRect.width + placementGap)
    var point = anchorPoint()
    previewCard.y = Math.round(Math.max(innerTop,
      Math.min(point.y, innerBottom - previewCard.height)))
    return true
  }

  function reposition() {
    if (!shown) return
    if (!placePreview()) dismissNow()
  }

  function loadArmedClip() {
    if (!requestFunction || !armedClipId) return
    var expectedId = armedClipId
    var expectedGeneration = generation
    requestPending = true
    requestFunction("clip.get", { id: expectedId }, function(clip) {
      if (generation !== expectedGeneration || armedClipId !== expectedId || !clip || String(clip.id) !== expectedId)
        return
      requestPending = false
      previewClip = clip
      previewScroll.contentY = 0
      if (placePreview()) shown = true
      else dismissNow()
    }, function(_) {
      if (generation === expectedGeneration && armedClipId === expectedId) {
        requestPending = false
        dismissNow()
      }
    })
  }

  function scrollToEnd() {
    previewScroll.contentY = scrollMaximum
  }

  function activeImageCount() {
    var count = 0
    for (var index = 0; index < previewAssets.count; index++) {
      var item = previewAssets.itemAt(index)
      if (item && item.imageActive) count++
    }
    return count
  }

  function maximumRequestedImageWidth() {
    var width = 0
    for (var index = 0; index < previewAssets.count; index++) {
      var item = previewAssets.itemAt(index)
      if (item && item.imageActive) width = Math.max(width, item.requestedDecodeWidth)
    }
    return width
  }

  onWidthChanged: if (shown) reposition()
  onHeightChanged: if (shown) reposition()
  onPreferredSideChanged: if (shown) reposition()
  onClipperRectChanged: if (shown) reposition()
  onUsableRectChanged: if (shown) reposition()
  onPreviewEnabledChanged: if (!previewEnabled) dismissNow()

  Timer { id: openTimer; interval: 300; onTriggered: root.loadArmedClip() }
  Timer {
    id: closeTimer
    interval: 260
    onTriggered: {
      if (!previewHover.hovered && !corridorHover.hovered) root.dismissNow()
    }
  }

  Item {
    id: corridor
    objectName: "hoverPreviewTransferCorridor"
    z: 999
    visible: root.shown && root.placementSide.length > 0
    x: root.placementSide === "left" ? previewCard.x + previewCard.width : root.clipperRect.x + root.clipperRect.width
    y: Math.min(previewCard.y, root.anchorPoint().y)
    width: root.placementGap
    height: Math.max(previewCard.y + previewCard.height,
      root.anchorPoint().y + (root.armedAnchor ? root.armedAnchor.height : 1)) - y

    HoverHandler {
      id: corridorHover
      onHoveredChanged: {
        if (hovered) root.keepOpen()
        else root.scheduleClose()
      }
    }
  }

  Rectangle {
    id: previewCard
    objectName: "hoverPreview"
    z: 1000
    visible: root.shown
    width: root.normalPreviewWidth
    height: root.normalPreviewHeight
    radius: 11
    color: root.surfaceColor
    border.color: root.borderColor
    clip: true

    layer.enabled: visible

    HoverHandler {
      id: previewHover
      onHoveredChanged: {
        if (hovered) root.keepOpen()
        else root.scheduleClose()
      }
    }

    ColumnLayout {
      anchors.fill: parent
      spacing: 0

      Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 58
        color: root.backgroundColor
        border.color: root.borderColor

        Column {
          anchors.fill: parent
          anchors.margins: 11
          spacing: 3
          Text {
            width: parent.width
            text: root.previewClip ? String(root.previewClip.title || translations.text("Untitled clip")) : ""
            color: root.foregroundColor
            font.family: root.fontFamily
            font.pixelSize: 14
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            objectName: "hoverPreviewCaption"
            width: parent.width
            text: root.previewClip
              ? translations.text(root.previewClip.kind === "article" ? "Saved article excerpt" : "Saved clip excerpt")
                + (root.previewClip.scope ? " · " + translations.scope(String(root.previewClip.scope)) : "")
              : ""
            color: root.mutedColor
            font.family: root.fontFamily
            font.pixelSize: 11
            elide: Text.ElideRight
          }
        }
      }

      Flickable {
        id: previewScroll
        objectName: "hoverPreviewScroll"
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        contentWidth: width
        contentHeight: previewContent.height + 32

        Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }

        Column {
          id: previewContent
          width: previewScroll.width
          topPadding: 16
          bottomPadding: 16
          spacing: 14

          Text {
            objectName: "hoverPreviewBody"
            width: parent.width - 32
            x: 16
            text: root.bodyText
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: root.foregroundColor
            linkColor: root.accentColor
            font.family: root.fontFamily
            font.pixelSize: 14
            lineHeight: 1.55
          }

          Column {
            id: previewAssetColumn
            width: previewContent.width - 32
            x: 16
            spacing: 14

            Repeater {
              id: previewAssets
              model: root.previewClip && Array.isArray(root.previewClip.assets) ? root.previewClip.assets : []
              delegate: Item {
                required property var modelData
                readonly property real contentY: previewAssetColumn.y + y
                readonly property bool nearViewport: contentY + height >= previewScroll.contentY - 40
                  && contentY <= previewScroll.contentY + previewScroll.height + 40
                readonly property bool imageActive: previewImageLoader.active && !!previewImageLoader.item
                readonly property int requestedDecodeWidth: Math.min(960, Math.max(1, Math.ceil(width * 2)))
                width: previewAssetColumn.width
                height: 220

                Loader {
                  id: previewImageLoader
                  anchors.fill: parent
                  active: parent.nearViewport && String(parent.modelData.url || "").length > 0
                  sourceComponent: Image {
                    source: String(modelData.url || "")
                    asynchronous: true
                    cache: false
                    fillMode: Image.PreserveAspectFit
                    sourceSize.width: requestedDecodeWidth
                    sourceSize.height: 520
                  }
                }

                Rectangle {
                  anchors.fill: parent
                  visible: String(modelData.url || "").length === 0
                    || (previewImageLoader.item && previewImageLoader.item.status === Image.Error)
                  radius: 6
                  color: root.backgroundColor
                  border.color: root.borderColor
                  Text {
                    anchors.centerIn: parent
                    width: parent.width - 24
                    text: translations.text("Saved image unavailable · remaining content is still readable")
                    color: root.mutedColor
                    font.family: root.fontFamily
                    font.pixelSize: 11
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
