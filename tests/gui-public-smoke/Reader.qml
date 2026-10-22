import QtQuick
import "../../ui" as Ui

Ui.Reader {
  id: harness

  Component.onCompleted: console.warn("GUI_SMOKE_WRAPPER_READY")

  property string smokeMode: argument("--smoke-mode")
  property string smokeBody: argument("--smoke-body")
  property string smokeNote: argument("--smoke-note")
  property string smokeVerifyNote: argument("--smoke-verify-note")
  property string smokeSavedNote: smokeNote
  property int smokePhase: 0
  property int smokeTicks: 0
  property var smokeArticleView: null
  property var smokeNoteEditor: null
  property var smokeSaveButton: null
  property var smokeCloseButton: null

  function argument(name) {
    var values = Qt.application.arguments
    for (var i = 0; i + 1 < values.length; i++)
      if (values[i] === name) return String(values[i + 1])
    return ""
  }

  function namedObject(value, name, depth) {
    if (!value || depth > 20) return null
    if (String(value.objectName || "") === name) return value
    var children = value.children
    if (children && children.length !== undefined) {
      for (var i = 0; i < children.length; i++) {
        var found = namedObject(children[i], name, depth + 1)
        if (found) return found
      }
    }
    if (value.contentItem) {
      var contentFound = namedObject(value.contentItem, name, depth + 1)
      if (contentFound) return contentFound
    }
    if (value.header) return namedObject(value.header, name, depth + 1)
    return null
  }

  function fail(message) {
    console.error("GUI_SMOKE_FAILURE:" + message)
    Qt.exit(91)
  }

  Timer {
    interval: 100
    repeat: true
    running: true
    onTriggered: {
      harness.smokeTicks++
      if (harness.smokeTicks > 300) {
        harness.fail("timeout at phase " + harness.smokePhase)
        return
      }

      if (harness.smokePhase === 0) {
        if (!harness.clip || harness.loading) return
        if (!harness.isArticle) {
          harness.fail("seeded clip did not open as an Article")
          return
        }
        harness.smokeArticleView = harness.namedObject(harness, "articleView", 0)
        harness.smokeNoteEditor = harness.namedObject(harness, "noteEditor", 0)
        harness.smokeSaveButton = harness.namedObject(harness, "saveNoteButton", 0)
        harness.smokeCloseButton = harness.namedObject(harness, "closeReaderButton", 0)
        if (!harness.smokeArticleView || !harness.smokeNoteEditor
            || !harness.smokeSaveButton || !harness.smokeCloseButton) return
        if (harness.smokeArticleView.loadProgress < 100) return
        harness.smokePhase = -1
        harness.smokeArticleView.findText(harness.smokeBody, 0, function(result) {
          if (!result || result.numberOfMatches < 1) {
            harness.fail("WebEngine could not find the captured Article body")
            return
          }
          console.log("GUI_SMOKE_BODY_MATCHES:" + result.numberOfMatches)
          harness.smokePhase = 1
        })
        return
      }

      if (harness.smokePhase === 1) {
        if (harness.smokeMode === "edit") {
          harness.smokeSavedNote = harness.smokeNote
          harness.smokeNoteEditor.text = harness.smokeNote
          if (!harness.noteDirty || !harness.smokeSaveButton.enabled) {
            harness.fail("editing the real note editor did not enable Save note")
            return
          }
          harness.smokeSaveButton.clicked()
          harness.smokePhase = 2
        } else if (harness.smokeMode === "verify") {
          if (String(harness.smokeNoteEditor.text) !== harness.smokeNote) {
            harness.fail("the reopened Reader did not show the saved note")
            return
          }
          console.log("GUI_SMOKE_NOTE_REOPENED")
          harness.smokeSavedNote = harness.smokeVerifyNote
          harness.smokeNoteEditor.text = harness.smokeVerifyNote
          if (!harness.noteDirty || !harness.smokeSaveButton.enabled) {
            harness.fail("the reopened note editor did not enable Save note")
            return
          }
          harness.smokeSaveButton.clicked()
          harness.smokePhase = 2
        } else {
          harness.fail("unknown smoke mode")
        }
        return
      }

      if (harness.smokePhase === 2) {
        if (harness.saving || harness.noteDirty) return
        if (!harness.clip || String(harness.clip.note || "") !== harness.smokeSavedNote) {
          harness.fail("Save note completed without updating the opened clip")
          return
        }
        console.log("GUI_SMOKE_NOTE_SAVED")
        harness.smokePhase = 3
        return
      }

      if (harness.smokePhase === 3) {
        console.log("GUI_SMOKE_CLOSE_CLICKED")
        harness.smokePhase = 4
        harness.smokeCloseButton.clicked()
      }
    }
  }
}
