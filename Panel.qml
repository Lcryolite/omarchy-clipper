import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Item {
  id: root
  objectName: "clipperPanel"

  property var bar: null
  property var settings: ({})
  property string moduleName: "lcryolite.clipper"
  property Item anchorItem: null
  property var hostWidget: null
  property bool opened: false
  property bool popoutSwitchClosing: false
  property bool settingsOpen: false
  property var preferenceQueue: []
  property var currentPreference: null
  property string preferenceError: ""
  property var effectiveSettings: ({})
  property bool preferencesReady: false
  property bool settingsReloadPending: false
  property bool identityReloadWaitingForDraft: false
  readonly property string settingsHelper: decodeURIComponent(Qt.resolvedUrl("clipper-settings.py").toString().replace(/^(file|qs):\/\//, ""))

  property string executable: effectiveSettings.executable || "clipper"
  property string dataDir: effectiveSettings.dataDir || ""
  property var requestQueue: []
  property var currentJob: null
  property string pendingPayload: ""

  property var clips: []
  property int totalClips: 0
  property int pageOffset: 0
  property int pageSize: 30
  property string filter: "all"
  property string query: ""
  property bool trashMode: false
  property bool listLoading: false
  property string listError: ""

  property string draftText: ""
  property var draftAssets: []
  property int draftRevision: 0
  property bool draftLoaded: false
  property bool draftDirty: false
  property bool draftSaving: false
  property bool draftSaveAgain: false
  property var afterDraftSave: []
  property string draftState: "Loading draft…"
  property string draftError: ""
  property bool committing: false
  property bool capturingRegion: false

  property color backgroundColor: Commons.Color.popups.background
  property color surfaceColor: Qt.rgba(backgroundColor.r, backgroundColor.g, backgroundColor.b, 0.78)
  property color foregroundColor: Commons.Color.popups.text
  property color mutedColor: Commons.Color.muted
  property color accentColor: Commons.Color.accent
  property color borderColor: Commons.Color.popups.border
  property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string resolvedLanguage: settingsPane.language === "system"
    ? (Qt.locale().name.toLowerCase().indexOf("zh") === 0 ? "zh" : "en")
    : settingsPane.language
  ClipperTranslations { id: translations; language: root.resolvedLanguage }

  readonly property bool draftEmpty: draftText.trim().length === 0 && draftAssets.length === 0
  readonly property bool searchHasFocus: searchInput.activeFocus
  readonly property bool composerHasFocus: composer.activeFocus

  function open() {
    opened = true
    refreshTheme()
    schedulePreferencesReload()
    Qt.callLater(function() { searchInput.forceActiveFocus() })
  }

  function close() {
    autosaveTimer.stop()
    externalPreview.controller.dismissNow()
    if (draftDirty) persistDraft(null)
    opened = false
  }

  function persistPreference(key, value) {
    preferenceError = ""
    preferenceQueue = preferenceQueue.concat([{ key: String(key), value: value }])
    startNextPreference()
  }

  function startNextPreference() {
    if (!preferencesReady || preferenceProcess.running || currentPreference || preferenceQueue.length === 0) return
    var next = preferenceQueue.slice()
    currentPreference = next.shift()
    preferenceQueue = next
    preferenceProcess.command = [
      "python3", settingsHelper, "set",
      currentPreference.key, JSON.stringify(currentPreference.value)
    ]
    preferenceProcess.running = true
  }

  function finishPreference(exitCode) {
    var saved = currentPreference
    currentPreference = null
    var output = String(preferenceStdout.text || "").trim()
    var parsed = null
    try { parsed = JSON.parse(output) } catch (_) {}
    if (exitCode !== 0 || !parsed) {
      preferenceError = String(preferenceStderr.text || output
        || "Could not save this setting.").trim()
      if (saved && settingsPane) {
        if (saved.key === "previewEnabled" && settingsPane.previewEnabledOverride === saved.value)
          settingsPane.previewEnabledOverride = undefined
        else if (saved.key === "previewSide" && settingsPane.previewSideOverride === saved.value)
          settingsPane.previewSideOverride = undefined
        else if (saved.key === "language" && settingsPane.languageOverride === saved.value)
          settingsPane.languageOverride = undefined
      }
    } else if (saved) {
      var updated = ({})
      for (var key in effectiveSettings) updated[key] = effectiveSettings[key]
      updated[saved.key] = parsed[saved.key]
      effectiveSettings = updated
    }
    Qt.callLater(startNextPreference)
  }

  function schedulePreferencesReload() {
    settingsReloadPending = true
    searchTimer.stop()
    reloadPreferences.restart()
  }

  function resetCollectionState() {
    autosaveTimer.stop()
    searchTimer.stop()
    externalPreview.controller.dismissNow()
    clips = []
    totalClips = 0
    pageOffset = 0
    filter = "all"
    query = ""
    trashMode = false
    listLoading = false
    listError = ""
    draftText = ""
    draftAssets = []
    draftRevision = 0
    draftLoaded = false
    draftDirty = false
    draftSaving = false
    draftSaveAgain = false
    afterDraftSave = []
    draftState = "Loading draft…"
    draftError = ""
    committing = false
    capturingRegion = false
  }

  function closeForPopoutSwitch() {
    popoutSwitchClosing = true
    close()
    switchCloseTimer.restart()
  }

  function request(op, fields, success, failure) {
    if (!preferencesReady) {
      if (failure) failure(preferenceError || "Settings are not ready. Close and reopen Clipper to retry.")
      return
    }
    var payload = { op: op }
    fields = fields || {}
    for (var key in fields) payload[key] = fields[key]
    requestQueue = requestQueue.concat([{
      payload: JSON.stringify(payload),
      success: success || null,
      failure: failure || null
    }])
    startNextRequest()
  }

  function startNextRequest() {
    if (requestProcess.running || currentJob || requestQueue.length === 0) return
    var next = requestQueue.slice()
    currentJob = next.shift()
    requestQueue = next
    pendingPayload = currentJob.payload + "\n"
    var args = [executable, "--request"]
    if (dataDir.length > 0) args = args.concat(["--data-dir", dataDir])
    requestProcess.command = args
    requestProcess.stdinEnabled = true
    requestProcess.running = true
  }

  function finishRequest(exitCode) {
    var job = currentJob
    currentJob = null
    var envelope = null
    var error = ""
    if (exitCode !== 0) {
      error = String(requestStderr.text || "Clipper exited with code " + exitCode).trim()
    } else {
      try { envelope = JSON.parse(String(requestStdout.text || "")) }
      catch (e) { error = "Clipper returned an invalid response."
      }
      if (!error && (!envelope || envelope.ok !== true))
        error = envelope && envelope.error ? String(envelope.error) : "Clipper rejected the request."
    }
    if (job) {
      if (error) { if (job.failure) job.failure(error) }
      else if (job.success) job.success(envelope.result)
    }
    Qt.callLater(startNextRequest)
  }

  function refreshTheme() {
    request("theme.get", {}, function(theme) {
      if (!theme) return
      if (theme.background) backgroundColor = theme.background
      if (theme.surface) surfaceColor = theme.surface
      if (theme.foreground) foregroundColor = theme.foreground
      if (theme.muted) mutedColor = theme.muted
      if (theme.accent) accentColor = theme.accent
      if (theme.border) borderColor = theme.border
      if (theme.fontFamily) fontFamily = theme.fontFamily
    }, function(_) {})
  }

  function loadDraft() {
    if (draftLoaded) return
    request("draft.get", {}, function(draft) {
      draftText = String(draft.text || "")
      draftAssets = Array.isArray(draft.assets) ? draft.assets : []
      if (draftAssets.length === 0 && Array.isArray(draft.assetIds)) {
        var recovered = []
        for (var i = 0; i < draft.assetIds.length; i++) recovered.push({ id: draft.assetIds[i], url: "", name: "Image" })
        draftAssets = recovered
      }
      draftRevision = Number(draft.revision || 0)
      draftLoaded = true
      draftDirty = false
      draftState = draftEmpty ? "Ready" : "Draft saved"
      draftError = ""
    }, function(error) {
      draftLoaded = true
      draftState = "Draft unavailable"
      draftError = error
    })
  }

  function assetIds() {
    var ids = []
    for (var i = 0; i < draftAssets.length; i++) if (draftAssets[i] && draftAssets[i].id) ids.push(String(draftAssets[i].id))
    return ids
  }

  function markDraftChanged() {
    if (!draftLoaded) return
    draftDirty = true
    draftState = "Saving draft…"
    draftError = ""
    autosaveTimer.restart()
  }

  function persistDraft(afterSave) {
    if (afterSave) afterDraftSave = afterDraftSave.concat([afterSave])
    if (!draftLoaded) return
    if (draftSaving) { draftSaveAgain = true; return }
    if (!draftDirty) {
      draftSaveAgain = false
      runAfterDraftSave()
      return
    }

    draftSaving = true
    draftSaveAgain = false
    var textSnapshot = draftText
    var idsSnapshot = assetIds()
    request("draft.save", {
      text: textSnapshot,
      assetIds: idsSnapshot,
      expectedRevision: draftRevision
    }, function(saved) {
      draftSaving = false
      draftRevision = Number(saved.revision || draftRevision)
      var unchanged = draftText === textSnapshot && JSON.stringify(assetIds()) === JSON.stringify(idsSnapshot)
      draftDirty = !unchanged
      draftState = unchanged ? "Draft saved" : "Saving draft…"
      draftError = ""
      if (draftDirty || draftSaveAgain) persistDraft(null)
      else runAfterDraftSave()
    }, function(error) {
      draftSaving = false
      committing = false
      draftDirty = true
      draftState = "Draft not saved"
      draftError = error
      afterDraftSave = []
      if (settingsReloadPending) {
        settingsReloadPending = false
        identityReloadWaitingForDraft = false
      }
    })
  }

  function runAfterDraftSave() {
    var actions = afterDraftSave.slice()
    afterDraftSave = []
    for (var i = 0; i < actions.length; i++) actions[i]()
  }

  function commitDraft() {
    if (draftEmpty || committing) return
    committing = true
    persistDraft(function() {
      request("draft.commit", { expectedRevision: draftRevision }, function(_) {
        committing = false
        draftText = ""
        draftAssets = []
        draftDirty = false
        draftState = "Saved"
        request("draft.get", {}, function(fresh) {
          draftRevision = Number(fresh.revision || draftRevision)
          draftState = "Ready"
        }, function(_) {})
        reloadClips()
        Qt.callLater(function() { composer.forceActiveFocus() })
      }, function(error) {
        committing = false
        draftState = "Save failed"
        draftError = error
      })
    })
  }

  function reloadClips() {
    externalPreview.controller.dismissNow()
    pageOffset = 0
    clips = []
    loadClips(false)
  }

  function loadClips(append) {
    if (listLoading) return
    listLoading = true
    listError = ""
    var requestedOffset = append ? clips.length : 0
    request("clip.list", {
      query: query,
      filter: filter,
      trash: trashMode,
      offset: requestedOffset,
      limit: pageSize
    }, function(result) {
      var items = result && Array.isArray(result.items) ? result.items : []
      clips = append ? clips.concat(items) : items
      totalClips = Number(result && result.total || 0)
      pageOffset = clips.length
      listLoading = false
    }, function(error) {
      listLoading = false
      listError = error
    })
  }

  function openClip(id) {
    request("clip.open", { id: id }, function(_) {}, function(error) { listError = error })
  }

  function trashClip(id) {
    request("clip.trash", { id: id }, function(_) { reloadClips() }, function(error) { listError = error })
  }

  function restoreClip(id) {
    request("clip.restore", { id: id }, function(_) { reloadClips() }, function(error) { listError = error })
  }

  function clipStateLabel(item) {
    var pending = item && item.pendingCapture ? item.pendingCapture : null
    var missing = []
    var pendingMissing = pending && pending.missing ? pending.missing : []
    var savedMissing = item && item.missing ? item.missing : []
    var seen = ({})
    for (var group of [pendingMissing, savedMissing]) {
      for (var i = 0; i < group.length; i++) {
        var problem = group[i]
        var key = String(problem.url || problem.reason || "")
        if (seen[key]) continue
        seen[key] = true
        missing.push(problem)
      }
    }
    var limited = missing.length > 0 && missing.every(function(problem) {
      return problem.retryable === false
    })
    var action = translations.text(limited ? "Content unavailable · Open details" : "Open to retry")
    if (pending) {
      if (pending.status === "incomplete")
        return translations.text(pending.isUpdate ? "Update incomplete · " : "Incomplete · ") + action
      return translations.text(pending.isUpdate ? "Update pending · Open source" : "Capture pending · Open source")
    }
    if (item && item.status === "incomplete") return translations.text("Incomplete · ") + action
    if (item && item.status === "capturing") return translations.text("Capture pending · Open source")
    return item && item.sourceUrl ? item.sourceUrl : translations.text(item && item.kind === "article" ? "Article" : "Mixed clip")
  }

  function clipNeedsAttention(item) {
    return !!(item && (item.pendingCapture || item.status === "incomplete" || item.status === "capturing"))
  }

  function emptyListMessage() {
    if (listError) return translations.error(listError)
    if (listLoading) return translations.text("Loading…")
    if (query.length > 0 || (!trashMode && filter !== "all")) return translations.text("No matching clips")
    return translations.text(trashMode ? "Trash is empty" : "Your collection is empty")
  }

  function importClipboard() {
    request("clipboard.import", {}, function(asset) {
      if (!asset || !asset.id) return
      draftAssets = draftAssets.concat([asset])
      markDraftChanged()
    }, function(_) {
      composer.paste()
    })
  }

  function importFiles(urls) {
    for (var i = 0; i < urls.length; i++) {
      var value = String(urls[i])
      var path = value.indexOf("file://") === 0 ? decodeURIComponent(value.substring(7)) : value
      request("file.import", { path: path }, function(asset) {
        if (!asset || !asset.id) return
        draftAssets = draftAssets.concat([asset])
        markDraftChanged()
      }, function(error) { draftError = error })
    }
  }

  function captureRegion() {
    if (capturingRegion) return
    autosaveTimer.stop()
    persistDraft(function() {
      capturingRegion = true
      opened = false
      captureDelay.restart()
    })
  }

  Process {
    id: settingsProcess
    stdout: StdioCollector { id: settingsStdout; waitForEnd: true }
    stderr: StdioCollector { id: settingsStderr; waitForEnd: true }
    onExited: function(exitCode) {
      try {
        if (exitCode !== 0) throw new Error(String(settingsStderr.text || "Could not load settings."))
        var next = JSON.parse(settingsStdout.text)
        var identityChanged = root.effectiveSettings.dataDir !== undefined
          && (root.dataDir !== String(next.dataDir || "")
            || root.executable !== String(next.executable || "clipper"))
        if (identityChanged) root.resetCollectionState()
        root.effectiveSettings = next
        root.preferencesReady = true
        root.settingsReloadPending = false
        root.preferenceError = ""
        if (root.opened) {
          if (identityChanged) root.refreshTheme()
          root.loadDraft()
          root.reloadClips()
        }
      } catch (error) {
        root.preferenceError = String(error)
        root.listError = root.preferenceError
        root.draftError = root.preferenceError
        root.clips = []
        root.settingsReloadPending = false
      }
    }
  }

  Timer {
    id: reloadPreferences
    interval: 30
    onTriggered: {
      if (!root.settingsReloadPending) return
      if (root.draftDirty || root.draftSaving || root.draftSaveAgain
          || autosaveTimer.running || root.afterDraftSave.length) {
        autosaveTimer.stop()
        if (!root.identityReloadWaitingForDraft) {
          root.identityReloadWaitingForDraft = true
          root.persistDraft(function() {
            root.identityReloadWaitingForDraft = false
            reloadPreferences.restart()
          })
        }
        return
      }
      if (requestProcess.running || root.currentJob || root.requestQueue.length
          || preferenceProcess.running || root.currentPreference || root.preferenceQueue.length
          || settingsProcess.running || captureDelay.running) {
        restart()
        return
      }
      root.preferencesReady = false
      settingsProcess.command = ["python3", root.settingsHelper, "load", "--legacy-json", JSON.stringify(root.settings || {})]
      settingsProcess.running = true
    }
  }

  Component.onCompleted: schedulePreferencesReload()

  Process {
    id: requestProcess
    running: false
    command: []
    stdinEnabled: false
    stdout: StdioCollector { id: requestStdout; waitForEnd: true }
    stderr: StdioCollector { id: requestStderr; waitForEnd: true }
    onStarted: {
      write(root.pendingPayload)
      root.pendingPayload = ""
      stdinEnabled = false
    }
    onExited: function(exitCode) { root.finishRequest(exitCode) }
  }

  Process {
    id: preferenceProcess
    running: false
    command: []
    stdout: StdioCollector { id: preferenceStdout; waitForEnd: true }
    stderr: StdioCollector { id: preferenceStderr; waitForEnd: true }
    onExited: function(exitCode) { root.finishPreference(exitCode) }
  }

  Timer { id: switchCloseTimer; interval: 1; onTriggered: root.popoutSwitchClosing = false }
  Timer { id: autosaveTimer; interval: 450; onTriggered: root.persistDraft(null) }
  Timer { id: searchTimer; interval: 220; onTriggered: root.reloadClips() }
  Timer {
    interval: 60000
    repeat: true
    running: root.opened
    onTriggered: root.refreshTheme()
  }
  Timer {
    id: captureDelay
    interval: 180
    onTriggered: root.request("capture.region", {}, function(asset) {
      root.capturingRegion = false
      if (asset && asset.id) {
        root.draftAssets = root.draftAssets.concat([asset])
        root.markDraftChanged()
      }
      root.opened = true
      Qt.callLater(function() { composer.forceActiveFocus() })
    }, function(error) {
      root.capturingRegion = false
      root.draftError = error
      root.opened = true
      Qt.callLater(function() { composer.forceActiveFocus() })
    })
  }

  KeyboardPanel {
    id: panel
    objectName: "clipperPopup"
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: root.settingsOpen ? settingsPane : searchInput
    contentWidth: fittedContentWidth(Style.space(460))
    contentHeight: fittedContentHeight(Style.space(root.settingsOpen ? 320 : 650), Style.space(680))

    Item {
      id: panelContent
      anchors.fill: parent
      focus: true
      Keys.onEscapePressed: function(event) { root.close(); event.accepted = true }

      ColumnLayout {
        anchors.fill: parent
        visible: !root.settingsOpen
        enabled: root.preferencesReady && !root.settingsReloadPending
        spacing: Style.space(8)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          PlainText {
            text: root.trashMode ? translations.text("Trash") : "Clipper"
            color: root.foregroundColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            font.bold: true
            Layout.fillWidth: true
          }

          PanelActionButton {
            id: settingsButton
            objectName: "settingsButton"
            Accessible.name: translations.text("Settings")
            Accessible.role: Accessible.Button
            iconText: "󰒓"
            tooltipText: translations.text("Settings")
            foreground: root.foregroundColor
            fontFamily: root.fontFamily
            focusable: true
            onClicked: {
              root.settingsOpen = true
              Qt.callLater(function() { settingsPane.focusReturnButton() })
            }
          }

          Rectangle {
            width: Style.space(34); height: Style.space(28)
            radius: Style.cornerRadius
            color: trashMouse.containsMouse ? root.accentColor : "transparent"
            border.color: root.borderColor
            PlainText {
              anchors.centerIn: parent
              text: root.trashMode ? "←" : "󰆴"
              color: trashMouse.containsMouse ? root.backgroundColor : root.foregroundColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
            }
            MouseArea {
              id: trashMouse
              anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
              onClicked: { root.trashMode = !root.trashMode; root.reloadClips() }
            }
          }
        }

        Controls.TextField {
          id: searchInput
          Layout.fillWidth: true
          implicitHeight: Style.space(36)
          placeholderText: translations.text(root.trashMode ? "Search Trash" : "Search clips")
          color: root.foregroundColor
          placeholderTextColor: root.mutedColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          selectByMouse: true
          background: Rectangle { color: "transparent"; border.color: root.borderColor; radius: Style.cornerRadius }
          onTextEdited: { root.query = text; searchTimer.restart() }
        }

        RowLayout {
          visible: !root.trashMode
          Layout.fillWidth: true
          spacing: Style.space(4)
          Repeater {
            model: [
              { key: "all", label: "All" },
              { key: "articles", label: "Articles" },
              { key: "notes", label: "Notes" },
              { key: "images", label: "Images" }
            ]
            delegate: Rectangle {
              required property var modelData
              Layout.fillWidth: true
              height: Style.space(30)
              radius: Style.cornerRadius
              color: root.filter === modelData.key ? root.accentColor : "transparent"
              PlainText {
                anchors.centerIn: parent
                text: translations.text(modelData.label)
                color: root.filter === modelData.key ? root.backgroundColor : root.mutedColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              MouseArea {
                anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                onClicked: { root.filter = modelData.key; root.reloadClips() }
              }
            }
          }
        }

        Item {
          Layout.fillWidth: true
          Layout.fillHeight: true
          Layout.minimumHeight: Style.space(220)

          ListView {
            id: clipList
            objectName: "clipList"
            anchors.fill: parent
            clip: true
            spacing: Style.space(7)
            model: root.clips
            boundsBehavior: Flickable.StopAtBounds
            onMovementStarted: externalPreview.controller.dismissNow()
            onAtYEndChanged: if (atYEnd && root.clips.length < root.totalClips) root.loadClips(true)

            delegate: ClipCard {
              width: clipList.width
              height: Style.space(82)
              previewController: externalPreview.controller
              language: root.resolvedLanguage
              trashMode: root.trashMode
              surfaceColor: root.surfaceColor
              foregroundColor: root.foregroundColor
              mutedColor: root.mutedColor
              accentColor: root.accentColor
              borderColor: root.borderColor
              fontFamily: root.fontFamily
              cornerRadius: Style.cornerRadius
              contentMargin: Style.space(8)
              contentSpacing: Style.space(9)
              coverWidth: Style.space(62)
              actionSize: Style.space(28)
              titleSize: Style.font.subtitle
              captionSize: Style.font.caption
              bodySize: Style.font.bodySmall
              iconSize: Style.font.display
              stateLabel: root.clipStateLabel(modelData)
              needsAttention: root.clipNeedsAttention(modelData)
              onOpenRequested: function(clipId) { root.openClip(clipId) }
              onLifecycleRequested: function(clipId) {
                if (root.trashMode) root.restoreClip(clipId)
                else root.trashClip(clipId)
              }
            }
          }

          PlainText {
            anchors.centerIn: parent
            width: parent.width - Style.space(30)
            horizontalAlignment: Text.AlignHCenter
            text: root.emptyListMessage()
            color: root.listError ? Commons.Color.urgent : root.mutedColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            visible: root.clips.length === 0
          }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }

        ColumnLayout {
          visible: !root.trashMode
          Layout.fillWidth: true
          spacing: Style.space(7)

          RowLayout {
            Layout.fillWidth: true
            PlainText {
              text: translations.text("New clip")
              color: root.foregroundColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
              Layout.fillWidth: true
            }
            PlainText {
              text: translations.text(root.draftState)
              color: root.draftState.indexOf("failed") >= 0 || root.draftState.indexOf("not saved") >= 0 ? Commons.Color.urgent : root.mutedColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(84)
            radius: Style.cornerRadius
            color: "transparent"
            border.color: composer.activeFocus ? root.accentColor : root.borderColor

            Controls.TextArea {
              id: composer
              objectName: "composer"
              anchors.fill: parent
              anchors.margins: 1
              padding: Style.space(8)
              text: root.draftText
              textFormat: TextEdit.PlainText
              wrapMode: TextEdit.Wrap
              color: root.foregroundColor
              placeholderText: translations.text("Write Markdown, paste text or images…")
              placeholderTextColor: root.mutedColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              selectByMouse: true
              enabled: root.preferencesReady && !root.settingsReloadPending && root.draftLoaded
              background: null
              onTextChanged: {
                if (root.draftLoaded && root.draftText !== text) {
                  root.draftText = text
                  root.markDraftChanged()
                }
              }
              Keys.onPressed: function(event) {
                if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                  root.commitDraft(); event.accepted = true
                } else if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_V) {
                  root.importClipboard(); event.accepted = true
                }
              }
            }

            DropArea {
              anchors.fill: parent
              onDropped: function(drop) {
                if (drop.hasUrls) { root.importFiles(drop.urls); drop.acceptProposedAction() }
              }
            }
          }

          ListView {
            id: attachmentList
            Layout.fillWidth: true
            Layout.preferredHeight: root.draftAssets.length > 0 ? Style.space(64) : 0
            visible: root.draftAssets.length > 0
            orientation: ListView.Horizontal
            spacing: Style.space(7)
            clip: true
            model: root.draftAssets
            delegate: Rectangle {
              required property var modelData
              required property int index
              width: Style.space(64); height: Style.space(60)
              radius: Style.cornerRadius
              color: root.surfaceColor
              border.color: root.borderColor
              clip: true
              Image { anchors.fill: parent; source: String(modelData.url || ""); fillMode: Image.PreserveAspectCrop; asynchronous: true }
              Rectangle {
                anchors.top: parent.top; anchors.right: parent.right
                width: Style.space(20); height: Style.space(20); radius: height / 2
                color: root.backgroundColor
                PlainText { anchors.centerIn: parent; text: "×"; color: root.foregroundColor; font.pixelSize: Style.font.title }
                MouseArea {
                  anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                  onClicked: {
                    var next = root.draftAssets.slice(); next.splice(index, 1)
                    root.draftAssets = next; root.markDraftChanged()
                  }
                }
              }
            }
          }

          PlainText {
            Layout.fillWidth: true
            visible: root.draftError.length > 0
            text: translations.error(root.draftError)
            color: Commons.Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            maximumLineCount: 2
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(7)
            Item { Layout.fillWidth: true }
            Rectangle {
              width: Style.space(40); height: Style.space(32); radius: Style.cornerRadius
              color: regionMouse.containsMouse ? Qt.rgba(root.foregroundColor.r, root.foregroundColor.g, root.foregroundColor.b, 0.12) : "transparent"
              border.color: root.borderColor
              PlainText { anchors.centerIn: parent; text: "󰹑"; color: root.foregroundColor; font.family: root.fontFamily; font.pixelSize: Style.font.title }
              MouseArea { id: regionMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.captureRegion() }
            }
            Rectangle {
              width: Style.space(92); height: Style.space(32); radius: Style.cornerRadius
              color: root.draftEmpty || root.committing ? Qt.rgba(root.accentColor.r, root.accentColor.g, root.accentColor.b, 0.35) : root.accentColor
              PlainText {
                anchors.centerIn: parent
                text: translations.text(root.committing ? "Saving…" : "Save")
                color: root.backgroundColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }
              MouseArea { anchors.fill: parent; enabled: !root.draftEmpty && !root.committing; cursorShape: Qt.PointingHandCursor; onClicked: root.commitDraft() }
            }
          }
        }

        Rectangle {
          visible: root.trashMode
          Layout.alignment: Qt.AlignRight
          width: Style.space(122); height: Style.space(32); radius: Style.cornerRadius
          color: emptyTrashMouse.containsMouse ? Commons.Color.urgent : "transparent"
          border.color: Commons.Color.urgent
          PlainText { anchors.centerIn: parent; text: translations.text("Empty Trash"); color: emptyTrashMouse.containsMouse ? root.backgroundColor : Commons.Color.urgent; font.family: root.fontFamily; font.pixelSize: Style.font.bodySmall }
          MouseArea { id: emptyTrashMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: emptyTrashDialog.open() }
        }
      }

      Rectangle {
        anchors.fill: parent
        z: 200
        visible: root.settingsOpen
        color: root.backgroundColor

        SettingsPane {
          id: settingsPane
          anchors.fill: parent
          anchors.margins: Style.spacing.panelPadding
          settings: root.effectiveSettings
          enabled: root.preferencesReady && !root.settingsReloadPending
          resolvedLanguage: root.resolvedLanguage
          persistFunction: function(key, value) { root.persistPreference(key, value) }
          foregroundColor: root.foregroundColor
          mutedColor: root.mutedColor
          fontFamily: root.fontFamily
          onCloseRequested: {
            root.settingsOpen = false
            Qt.callLater(function() { settingsButton.forceActiveFocus() })
          }
        }

        PlainText {
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          anchors.margins: Style.space(8)
          visible: root.preferenceError.length > 0
          text: translations.error(root.preferenceError)
          color: Commons.Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          maximumLineCount: 2
        }
      }

      Controls.Dialog {
        id: emptyTrashDialog
        anchors.centerIn: parent
        width: Style.space(320)
        modal: true
        title: translations.text("Permanently empty Trash?")
        footer: Controls.DialogButtonBox {
          Controls.Button {
            text: translations.text("Yes")
            Controls.DialogButtonBox.buttonRole: Controls.DialogButtonBox.AcceptRole
          }
          Controls.Button {
            text: translations.text("Cancel")
            Controls.DialogButtonBox.buttonRole: Controls.DialogButtonBox.RejectRole
          }
          onAccepted: emptyTrashDialog.accept()
          onRejected: emptyTrashDialog.reject()
        }
        onAccepted: root.request("trash.empty", { confirm: true }, function(_) { root.reloadClips() }, function(error) { root.listError = error })
        background: Rectangle { color: root.backgroundColor; border.color: root.borderColor; radius: Style.cornerRadius }
        contentItem: PlainText {
          text: translations.text("This permanently removes every clip in Trash and cannot be undone.")
          color: root.foregroundColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }
      }
    }
  }

  HoverPreviewWindow {
    id: externalPreview
    panelWindow: panel
    language: root.resolvedLanguage
    previewEnabled: settingsPane.previewEnabled
    preferredSide: settingsPane.previewSide
    requestFunction: function(op, fields, success, failure) {
      root.request(op, fields, success, failure)
    }
    backgroundColor: root.backgroundColor
    surfaceColor: root.surfaceColor
    foregroundColor: root.foregroundColor
    mutedColor: root.mutedColor
    accentColor: root.accentColor
    borderColor: root.borderColor
    fontFamily: root.fontFamily
  }
}
