import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import QtWebEngine

Controls.ApplicationWindow {
  id: window
  objectName: "readerWindow"
  width: 1120
  height: 760
  minimumWidth: 620
  minimumHeight: 480
  visible: true
  title: clip && clip.kind === "article" ? qsTr("Clipper — Reader") : qsTr("Clipper — Detail")
  color: backgroundColor

  property var clip: null
  property string errorMessage: ""
  property string backendErrorMessage: ""
  property string statusMessage: ""
  property bool loading: true
  property bool allowClose: false
  property bool editingMixed: false
  property string editText: ""
  property var editAssets: []
  property string noteText: ""
  property bool mixedDirty: false
  property bool noteDirty: false
  property bool saving: false
  property var selectedAsset: null
  property string pendingExitAction: "close"

  property color backgroundColor: "#101315"
  property color surfaceColor: "#192024"
  property color foregroundColor: "#e5e5e5"
  property color mutedColor: "#8b949e"
  property color accentColor: "#91c7b1"
  property color borderColor: "#50606a"
  property string themeFont: "monospace"
  property string themeFingerprint: ""
  property int previewGeneration: 0

  readonly property bool dirty: mixedDirty || noteDirty
  readonly property bool isArticle: clip && clip.kind === "article"
  readonly property bool isMixed: clip && clip.kind === "mixed"
  readonly property var pendingCapture: clip && clip.pendingCapture ? clip.pendingCapture : null
  readonly property bool hasRetryState: !!pendingCapture || (clip && (clip.status === "incomplete" || clip.status === "capturing"))
  readonly property var captureProblems: buildCaptureProblems()
  readonly property bool canRetryCapture: captureProblems.length === 0 || captureProblems.some(function(problem) { return problem.retryable })

  function sequenceCopy(value) {
    if (!value || value.length === undefined) return []
    var result = []
    for (var i = 0; i < Number(value.length); i++) result.push(value[i])
    return result
  }

  function buildCaptureProblems() {
    var currentLanguage = bridge.resolvedLanguage
    var result = []
    var seen = ({})
    var seenReasons = ({})
    function add(value) {
      if (!value || result.length >= 20) return
      var rawReason = String(value.reason || "")
      var reason = rawReason.length > 0 ? bridge.localizedMessage(rawReason) : qsTr("Content could not be captured")
      var retryable = value.retryable !== false
      var normalized = { url: String(value.url || ""), reason: reason, retryable: retryable }
      var key = normalized.url || normalized.reason
      if (seen[key]) return
      seen[key] = true
      seenReasons[rawReason || normalized.reason] = true
      result.push(normalized)
    }
    var pending = clip && clip.pendingCapture ? clip.pendingCapture : null
    var pendingMissing = sequenceCopy(pending ? pending.missing : null)
    for (var i = 0; i < pendingMissing.length; i++) add(pendingMissing[i])
    var missing = sequenceCopy(clip ? clip.missing : null)
    for (var j = 0; j < missing.length; j++) add(missing[j])
    if (pending && pending.reason && !seenReasons[String(pending.reason)])
      add({ url: pending.sourceUrl || clip.sourceUrl || "", reason: pending.reason })
    return result
  }

  function captureStateLabel() {
    if (pendingCapture) {
      if (pendingCapture.status === "incomplete")
        return pendingCapture.isUpdate ? qsTr("Update interrupted") : qsTr("Incomplete capture")
      return pendingCapture.isUpdate ? qsTr("Update pending") : qsTr("Capture pending")
    }
    if (clip && clip.status === "incomplete") return qsTr("Incomplete capture")
    if (clip && clip.status === "capturing") return qsTr("Capture pending")
    if (!clip) return ""
    if (!clip.status || clip.status === "ready") return qsTr("Ready")
    return String(clip.status)
  }

  function captureScopeLabel() {
    if (!clip || !clip.scope) return qsTr("Saved article")
    var scope = String(clip.scope)
    if (scope.indexOf("selection-") === 0) return qsTr("Selected content")
    var known = {
      "article": qsTr("Article"),
      "github-readme": qsTr("GitHub README"),
      "github-issue-loaded": qsTr("GitHub Issue discussion"),
      "github-pr-loaded": qsTr("GitHub pull request discussion"),
      "discuz-main-post": qsTr("Discuz original post"),
      "discuz-current-page": qsTr("Discuz current-page discussion"),
      "discuz-current-page-replies": qsTr("Discuz visible replies"),
      "forum-current-page": qsTr("Forum current page"),
      "page-index": qsTr("Page index")
    }
    return known[scope] || scope
  }

  function retrySource() {
    var source = pendingCapture && pendingCapture.sourceUrl ? pendingCapture.sourceUrl : (clip ? clip.sourceUrl : "")
    if (!source) {
      errorMessage = qsTr("This capture has no source URL to reopen.")
      return
    }
    openExternal(source)
    statusMessage = canRetryCapture ? qsTr("Use the browser Clipper button and choose Update to retry")
      : qsTr("This content cannot be retried automatically. Use a selection or screenshot.")
    statusTimer.restart()
  }

  function call(op, fields) {
    var request = { op: op }
    fields = fields || {}
    for (var key in fields) request[key] = fields[key]
    var response
    try { response = bridge.request(request) }
    catch (e) {
      errorMessage = String(e)
      return null
    }
    if (response && response.ok === false) {
      backendErrorMessage = String(response.error || "")
      errorMessage = backendErrorMessage.length > 0 ? bridge.localizedMessage(backendErrorMessage) : qsTr("Clipper rejected the request.")
      return null
    }
    backendErrorMessage = ""
    errorMessage = ""
    return response && response.ok === true && response.result !== undefined ? response.result : response
  }

  function applyTheme() {
    var theme = call("theme.get", {})
    if (!theme) return
    var fingerprint = JSON.stringify(theme)
    var changed = themeFingerprint.length > 0 && themeFingerprint !== fingerprint
    themeFingerprint = fingerprint
    if (theme.background) backgroundColor = theme.background
    if (theme.surface) surfaceColor = theme.surface
    if (theme.foreground) foregroundColor = theme.foreground
    if (theme.muted) mutedColor = theme.muted
    if (theme.accent) accentColor = theme.accent
    if (theme.border) borderColor = theme.border
    if (theme.fontFamily) themeFont = theme.fontFamily
    if (changed) previewGeneration++
  }

  function loadClip() {
    loading = true
    var loaded = call("clip.get", { id: String(bridge.initialClipId || "") })
    loading = false
    if (!loaded) return
    clip = loaded
    editText = String(clip.text || "")
    editAssets = sequenceCopy(clip.assets)
    noteText = String(clip.note || "")
    mixedDirty = false
    noteDirty = false
    if (clip.kind === "article") articleView.reload()
    else if (clip.kind === "mixed") mixedPreview.reload()
  }

  function openExternal(url) {
    if (!url) return
    call("link.open", { url: String(url) })
  }

  function currentReaderUrl() {
    if (!clip || !clip.id) return "about:blank"
    var value = String(bridge.readerUrl(String(clip.id)))
    return value + (value.indexOf("?") >= 0 ? "&" : "?") + "ui=" + previewGeneration
  }

  function saveMixed(closeAfter) {
    if (!clip || !mixedDirty) {
      if (closeAfter) finishPendingExit()
      return
    }
    saving = true
    var ids = []
    for (var i = 0; i < editAssets.length; i++) if (editAssets[i] && editAssets[i].id) ids.push(String(editAssets[i].id))
    var saved = call("clip.edit", {
      id: clip.id,
      text: editText,
      assetIds: ids,
      expectedRevision: clip.revision
    })
    saving = false
    if (!saved) return
    clip = saved
    editText = String(saved.text || "")
    editAssets = sequenceCopy(saved.assets)
    mixedDirty = false
    statusMessage = qsTr("Changes saved")
    statusTimer.restart()
    mixedPreview.reload()
    if (closeAfter) finishPendingExit()
  }

  function saveNote(closeAfter) {
    if (!clip || !noteDirty) {
      if (closeAfter) finishPendingExit()
      return
    }
    saving = true
    var saved = call("note.save", {
      id: clip.id,
      note: noteText,
      expectedRevision: clip.revision
    })
    saving = false
    if (!saved) return
    clip = saved
    noteText = String(saved.note || noteText)
    noteDirty = false
    statusMessage = qsTr("Note saved")
    statusTimer.restart()
    if (closeAfter) finishPendingExit()
  }

  function saveAndClose() {
    if (isArticle) saveNote(true)
    else saveMixed(true)
  }

  function discardChanges() {
    if (!clip) return
    editText = String(clip.text || "")
    editAssets = sequenceCopy(clip.assets)
    noteText = String(clip.note || "")
    mixedDirty = false
    noteDirty = false
  }

  function closeNow() {
    allowClose = true
    window.close()
  }

  function requestClose() {
    pendingExitAction = "close"
    if (dirty) closeDialog.open()
    else closeNow()
  }

  function trashClip() {
    if (!clip) return
    pendingExitAction = clip.deletedAt ? "restore" : "trash"
    if (dirty) {
      closeDialog.open()
      return
    }
    performLifecycleAction()
  }

  function performLifecycleAction() {
    if (!clip) return
    var result = call(clip.deletedAt ? "clip.restore" : "clip.trash", { id: clip.id })
    if (!result) return
    if (clip.deletedAt) loadClip()
    else closeNow()
    pendingExitAction = "close"
  }

  function finishPendingExit() {
    if (pendingExitAction === "trash" || pendingExitAction === "restore") performLifecycleAction()
    else closeNow()
  }

  function copyClip(assetId) {
    if (!clip) return
    var fields = { id: clip.id }
    if (assetId) fields.assetId = assetId
    var copied = call("clipboard.copy", fields)
    if (copied === null) return
    statusMessage = assetId ? qsTr("Image copied") : qsTr("Clip copied")
    statusTimer.restart()
  }

  Component.onCompleted: {
    applyTheme()
    loadClip()
  }

  Connections {
    target: bridge
    function onLanguageChanged() {
      statusMessage = ""
      errorMessage = backendErrorMessage.length > 0 ? bridge.localizedMessage(backendErrorMessage) : ""
      previewGeneration++
      imageDialog.updateCloseButton()
    }
  }

  onClosing: function(close) {
    if (allowClose) return
    close.accepted = false
    requestClose()
  }

  Timer { interval: 30000; repeat: true; running: window.visible; onTriggered: window.applyTheme() }
  Timer { id: statusTimer; interval: 2500; onTriggered: window.statusMessage = "" }

  header: Rectangle {
    height: 58
    color: surfaceColor
    border.color: borderColor

    RowLayout {
      anchors.fill: parent
      anchors.leftMargin: 18
      anchors.rightMargin: 18
      spacing: 10

      PlainText {
        text: window.clip ? String(window.clip.title || (window.isArticle ? qsTr("Untitled article") : qsTr("Untitled clip"))) : "Clipper"
        color: foregroundColor
        font.family: themeFont
        font.pixelSize: 16
        font.bold: true
        maximumLineCount: 1
        Layout.fillWidth: true
      }

      PlainText {
        visible: statusMessage.length > 0
        text: statusMessage
        color: accentColor
        font.family: themeFont
        font.pixelSize: 12
        maximumLineCount: 1
      }

      ActionButton {
        objectName: "openSourceButton"
        visible: !!(window.clip && window.clip.sourceUrl)
        text: qsTr("Open source")
        foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont
        onClicked: window.openExternal(window.clip.sourceUrl)
      }
      ActionButton {
        objectName: "refreshSourceButton"
        visible: window.isArticle && !!(window.clip && window.clip.sourceUrl)
        text: hasRetryState && window.canRetryCapture ? qsTr("Retry source") : qsTr("Refresh")
        foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont
        onClicked: {
          window.retrySource()
        }
      }
      ActionButton {
        objectName: "trashButton"
        text: window.clip && window.clip.deletedAt ? qsTr("Restore") : qsTr("Trash")
        foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont
        onClicked: window.trashClip()
      }
    }
  }

  Item {
    anchors.fill: parent

    ColumnLayout {
      anchors.fill: parent
      anchors.margins: 16
      spacing: 10

      PlainText {
        visible: errorMessage.length > 0
        Layout.fillWidth: true
        text: errorMessage
        color: "#e06c75"
        font.family: themeFont
        font.pixelSize: 12
        maximumLineCount: 3
      }

      PlainText {
        visible: loading
        Layout.alignment: Qt.AlignCenter
        text: qsTr("Loading clip…")
        color: mutedColor
        font.family: themeFont
        font.pixelSize: 14
      }

      ColumnLayout {
        visible: isArticle
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: 8

        RowLayout {
          Layout.fillWidth: true
          PlainText {
            Layout.fillWidth: true
            text: window.clip ? [window.captureScopeLabel(), window.captureStateLabel()].join(" · ") : ""
            color: hasRetryState ? accentColor : mutedColor
            font.family: themeFont
            font.pixelSize: 12
            maximumLineCount: 1
          }
          PlainText {
            text: window.clip && window.clip.createdAt ? String(window.clip.createdAt) : ""
            color: mutedColor
            font.family: themeFont
            font.pixelSize: 12
            maximumLineCount: 1
          }
        }

        Rectangle {
          visible: window.hasRetryState || window.captureProblems.length > 0
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(124, 56 + window.captureProblems.length * 38)
          color: surfaceColor
          border.color: accentColor
          radius: 5

          ColumnLayout {
            anchors.fill: parent
            anchors.margins: 9
            spacing: 5

            RowLayout {
              Layout.fillWidth: true
              PlainText {
                Layout.fillWidth: true
                text: window.captureStateLabel() + (window.captureProblems.length > 0
                  ? " · " + window.captureProblems.length + (window.captureProblems.length === 1 ? qsTr(" issue") : qsTr(" issues")) : "")
                color: accentColor
                font.family: themeFont
                font.pixelSize: 12
                font.bold: true
                maximumLineCount: 1
              }
              ActionButton {
                objectName: "retrySourceButton"
                visible: !!(window.pendingCapture && window.pendingCapture.sourceUrl) || !!(window.clip && window.clip.sourceUrl)
                text: window.canRetryCapture ? qsTr("Open source to retry") : qsTr("Open source")
                foreground: backgroundColor; fillColor: accentColor; hoverBackground: Qt.lighter(accentColor, 1.1); borderColor: accentColor; fontFamily: themeFont
                onClicked: window.retrySource()
              }
            }

            PlainText {
              visible: window.captureProblems.length === 0
              Layout.fillWidth: true
              text: qsTr("The capture has not finished. Reopen the source and choose Update from the browser Clipper button.")
              color: mutedColor
              font.family: themeFont
              font.pixelSize: 11
              maximumLineCount: 2
            }

            ListView {
              id: captureProblemList
              objectName: "captureProblemList"
              visible: window.captureProblems.length > 0
              Layout.fillWidth: true
              Layout.fillHeight: true
              clip: true
              spacing: 4
              model: window.captureProblems
              boundsBehavior: Flickable.StopAtBounds
              delegate: Column {
                required property var modelData
                width: captureProblemList.width
                spacing: 1
                PlainText {
                  width: parent.width
                  text: String(modelData.reason || qsTr("Content could not be captured"))
                  color: foregroundColor
                  font.family: themeFont
                  font.pixelSize: 11
                  maximumLineCount: 1
                }
                PlainText {
                  visible: String(modelData.url || "").length > 0
                  width: parent.width
                  text: String(modelData.url || "")
                  color: mutedColor
                  font.family: themeFont
                  font.pixelSize: 10
                  maximumLineCount: 1
                }
              }
            }
          }
        }

        GridLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          columns: window.width >= 900 ? 2 : 1
          columnSpacing: 12
          rowSpacing: 12

          Rectangle {
            objectName: "articlePanel"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: window.width >= 900 ? 700 : -1
            Layout.minimumHeight: window.width >= 900 ? 200 : 300
            color: surfaceColor
            border.color: borderColor
            radius: 5
            clip: true

            WebEngineView {
              id: articleView
              objectName: "articleView"
              anchors.fill: parent
              anchors.margins: 1
              url: window.currentReaderUrl()
              backgroundColor: window.backgroundColor
              // Captured scripts never survive sanitization. JavaScript is enabled only
              // for the fixed reader-owned Copy and Wrap controls in clipper:// pages.
              settings.javascriptEnabled: true
              settings.javascriptCanAccessClipboard: true
              settings.localStorageEnabled: false
              settings.pluginsEnabled: false
              settings.fullScreenSupportEnabled: false
              settings.localContentCanAccessRemoteUrls: false
              settings.localContentCanAccessFileUrls: false
              settings.errorPageEnabled: false
              onNavigationRequested: function(request) {
                var target = String(request.url)
                if (target.indexOf("clipper://") === 0 || target === "about:blank") {
                  request.accept()
                } else {
                  request.reject()
                  if (request.navigationType === WebEngineNavigationRequest.LinkClickedNavigation)
                    window.openExternal(request.url)
                }
              }
              onNewWindowRequested: function(request) {
                window.openExternal(request.requestedUrl)
              }
              onLoadingChanged: function(info) {
                if (info.status === WebEngineView.LoadFailedStatus)
                window.errorMessage = qsTr("Could not display the saved article: %1").arg(String(info.errorString || qsTr("unknown reader error")))
              }
            }
          }

          Rectangle {
            objectName: "notePanel"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: window.width >= 900 ? 350 : -1
            Layout.minimumHeight: window.width >= 900 ? 200 : 230
            color: surfaceColor
            border.color: borderColor
            radius: 5

            ColumnLayout {
              anchors.fill: parent
              anchors.margins: 12
              spacing: 8
              PlainText {
                text: qsTr("My notes")
                color: foregroundColor
                font.family: themeFont
                font.pixelSize: 17
                font.bold: true
              }
              Controls.TextArea {
                id: noteEditor
                objectName: "noteEditor"
                Layout.fillWidth: true
                Layout.fillHeight: true
                text: noteText
                textFormat: TextEdit.PlainText
                wrapMode: TextEdit.Wrap
                color: foregroundColor
                placeholderText: qsTr("Add personal Markdown notes…")
                placeholderTextColor: mutedColor
                font.family: themeFont
                font.pixelSize: 13
                selectByMouse: true
                background: Rectangle { color: "transparent"; border.color: borderColor; radius: 4 }
                onTextChanged: if (window.clip && window.noteText !== text) { window.noteText = text; window.noteDirty = true }
                Keys.onPressed: function(event) {
                  if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                    window.saveNote(false); event.accepted = true
                  }
                }
              }
              RowLayout {
                Layout.fillWidth: true
                PlainText {
                  Layout.fillWidth: true
                  text: noteDirty ? qsTr("Unsaved changes") : qsTr("Saved")
                  color: noteDirty ? accentColor : mutedColor
                  font.family: themeFont
                  font.pixelSize: 11
                }
                ActionButton {
                  objectName: "saveNoteButton"
                  text: saving ? qsTr("Saving…") : qsTr("Save note")
                  enabled: noteDirty && !saving
                  foreground: backgroundColor; fillColor: accentColor; hoverBackground: Qt.lighter(accentColor, 1.1); borderColor: accentColor; fontFamily: themeFont
                  onClicked: window.saveNote(false)
                }
              }
            }
          }
        }
      }

      ColumnLayout {
        visible: isMixed
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: 10

        RowLayout {
          Layout.fillWidth: true
          ActionButton {
            objectName: "editMixedButton"
            text: editingMixed ? qsTr("Preview") : qsTr("Edit")
            foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont
            onClicked: editingMixed = !editingMixed
          }
          ActionButton {
            objectName: "copyClipButton"
            text: qsTr("Copy clip")
            foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont
            onClicked: window.copyClip("")
          }
          PlainText {
            Layout.fillWidth: true
            text: mixedDirty ? (editingMixed ? qsTr("Unsaved changes") : qsTr("Save to refresh preview")) : (editingMixed ? qsTr("Markdown editor") : qsTr("Rendered preview"))
            color: mixedDirty ? accentColor : mutedColor
            font.family: themeFont
            font.pixelSize: 12
            horizontalAlignment: Text.AlignRight
            maximumLineCount: 1
          }
          ActionButton {
            objectName: "saveMixedButton"
            text: saving ? qsTr("Saving…") : qsTr("Save")
            enabled: mixedDirty && !saving
            foreground: backgroundColor; fillColor: accentColor; hoverBackground: Qt.lighter(accentColor, 1.1); borderColor: accentColor; fontFamily: themeFont
            onClicked: window.saveMixed(false)
          }
        }

        Rectangle {
          Layout.fillWidth: true
          Layout.fillHeight: true
          color: surfaceColor
          border.color: borderColor
          radius: 5
          clip: true

          Controls.TextArea {
            objectName: "mixedEditor"
            anchors.fill: parent
            anchors.margins: 1
            visible: editingMixed
            padding: 14
            text: editText
            textFormat: TextEdit.PlainText
            wrapMode: TextEdit.Wrap
            color: foregroundColor
            font.family: themeFont
            font.pixelSize: 14
            selectByMouse: true
            background: null
            onTextChanged: if (window.clip && window.editText !== text) { window.editText = text; window.mixedDirty = true }
            Keys.onPressed: function(event) {
              if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                window.saveMixed(false); event.accepted = true
              }
            }
          }

          WebEngineView {
            id: mixedPreview
            objectName: "mixedPreview"
            anchors.fill: parent
            anchors.margins: 1
            visible: !editingMixed
            url: window.currentReaderUrl()
            backgroundColor: window.backgroundColor
            settings.javascriptEnabled: false
            settings.localStorageEnabled: false
            settings.pluginsEnabled: false
            settings.localContentCanAccessRemoteUrls: false
            settings.localContentCanAccessFileUrls: false
            settings.errorPageEnabled: false
            onNavigationRequested: function(request) {
              var target = String(request.url)
              if (target.indexOf("clipper://") === 0 || target === "about:blank") request.accept()
              else {
                request.reject()
                if (request.navigationType === WebEngineNavigationRequest.LinkClickedNavigation) window.openExternal(request.url)
              }
            }
            onLoadingChanged: function(info) {
              if (info.status === WebEngineView.LoadFailedStatus)
              window.errorMessage = qsTr("Could not display the saved clip preview: %1").arg(String(info.errorString || qsTr("unknown reader error")))
            }
          }
        }

        ListView {
          id: assetStrip
          Layout.fillWidth: true
          Layout.preferredHeight: editAssets.length > 0 ? 116 : 0
          visible: editAssets.length > 0
          orientation: ListView.Horizontal
          spacing: 9
          clip: true
          model: editAssets
          delegate: Rectangle {
            required property var modelData
            required property int index
            width: 142; height: 108
            radius: 5
            color: surfaceColor
            border.color: borderColor
            clip: true

            Image {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: imageActions.top
              source: String(modelData.url || "")
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
            }
            ActionButton {
              objectName: "viewImageButton"
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: imageActions.top
              text: ""
              accessibleName: qsTr("View %1 full size").arg(String(modelData.name || qsTr("image")))
              foreground: accentColor
              fillColor: "transparent"
              hoverBackground: Qt.rgba(accentColor.r, accentColor.g, accentColor.b, 0.18)
              borderColor: "transparent"
              onClicked: { selectedAsset = modelData; imageDialog.open() }
            }
            Rectangle {
              id: imageActions
              anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
              height: 30
              color: Qt.rgba(backgroundColor.r, backgroundColor.g, backgroundColor.b, 0.88)
              RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 5
                anchors.rightMargin: 3
                spacing: 3
                PlainText {
                  Layout.fillWidth: true
                  text: String(modelData.name || qsTr("Image"))
                  color: foregroundColor
                  font.family: themeFont
                  font.pixelSize: 10
                  maximumLineCount: 1
                }
                ActionButton {
                  objectName: "copyImageButton"
                  implicitWidth: 36
                  implicitHeight: 24
                  text: qsTr("Copy")
                  fontPixelSize: 10
                  foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont
                  onClicked: window.copyClip(modelData.id)
                }
                ActionButton {
                  objectName: "removeImageButton"
                  visible: editingMixed
                  implicitWidth: 47
                  implicitHeight: 24
                  text: qsTr("Remove")
                  fontPixelSize: 10
                  foreground: "#e06c75"; borderColor: borderColor; hoverBackground: "#5a3035"; fontFamily: themeFont
                  onClicked: {
                    var next = editAssets.slice(); next.splice(index, 1)
                    editAssets = next; mixedDirty = true
                  }
                }
              }
            }
          }
        }
      }
    }

    Controls.Dialog {
      id: closeDialog
      objectName: "closeDialog"
      anchors.centerIn: parent
      modal: true
      closePolicy: Controls.Popup.NoAutoClose
      onOpened: cancelExitButton.forceActiveFocus(Qt.PopupFocusReason)
      background: Rectangle { color: surfaceColor; border.color: borderColor; radius: 6 }
      contentItem: ColumnLayout {
        width: 420
        spacing: 16
        PlainText {
          Layout.fillWidth: true
          text: pendingExitAction === "trash" ? qsTr("Save changes before moving to Trash?") : (pendingExitAction === "restore" ? qsTr("Save changes before restoring?") : qsTr("Save changes before closing?"))
          color: foregroundColor
          font.family: themeFont
          font.pixelSize: 16
          font.bold: true
        }
        PlainText {
          Layout.fillWidth: true
          text: qsTr("Choose Save to keep your edits, Discard to close without them, or Cancel to continue editing.")
          color: mutedColor
          font.family: themeFont
          font.pixelSize: 12
        }
        RowLayout {
          Layout.alignment: Qt.AlignRight
          spacing: 8
          ActionButton { id: cancelExitButton; objectName: "cancelExitButton"; text: qsTr("Cancel"); foreground: foregroundColor; borderColor: borderColor; hoverBackground: accentColor; fontFamily: themeFont; onClicked: { closeDialog.close(); pendingExitAction = "close" } }
          ActionButton {
            objectName: "discardExitButton"
            text: qsTr("Discard"); foreground: foregroundColor; borderColor: borderColor; hoverBackground: "#e06c75"; fontFamily: themeFont
            onClicked: { closeDialog.close(); window.discardChanges(); window.finishPendingExit() }
          }
          ActionButton {
            objectName: "saveExitButton"
            text: qsTr("Save"); foreground: backgroundColor; fillColor: accentColor; hoverBackground: Qt.lighter(accentColor, 1.1); borderColor: accentColor; fontFamily: themeFont
            onClicked: { closeDialog.close(); window.saveAndClose() }
          }
        }
      }
    }

    Controls.Dialog {
      id: imageDialog
      objectName: "imageDialog"
      anchors.centerIn: parent
      modal: true
      width: Math.min(window.width - 60, selectedAsset ? 980 : 300)
      height: Math.min(window.height - 60, selectedAsset ? 700 : 240)
      standardButtons: Controls.Dialog.Close
      function updateCloseButton() {
        var button = standardButton(Controls.Dialog.Close)
        if (button) button.text = qsTr("Close")
      }
      onOpened: updateCloseButton()
      background: Rectangle { color: backgroundColor; border.color: borderColor; radius: 6 }
      contentItem: Image {
        objectName: "fullSizeImage"
        source: selectedAsset ? String(selectedAsset.url || "") : ""
        fillMode: Image.PreserveAspectFit
        asynchronous: true
      }
    }
  }
}
