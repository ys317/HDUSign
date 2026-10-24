import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: window
    objectName: "mainWindow"
    visible: true
    width: 1180; height: 760
    minimumWidth: 960; minimumHeight: 600
    // Keep the native title bar quiet; version and executable identity belong
    // in diagnostics/about information rather than the working surface.
    title: ""
    color: Theme.canvas
    property bool collapsed: false
    property bool settingsVisible: false
    property bool activityExpanded: false
    property bool followLogs: true
    readonly property real pageMargin: main.width < 800 ? 32 : 48
    readonly property real pageWidth: Math.min(1120, main.width - pageMargin * 2)
    readonly property string coursePageTitle: !appController.form.live ? "电脑声音" : appController.state.courseTitle || (appController.state.selectedUrl ? "直播课堂" : "课堂监控")
    readonly property bool courseProblem: appController.state.courseLoadState === "error"
    readonly property bool showCourseNotice: appController.form.live && !appController.state.monitoring && !appController.state.scheduled && (courseProblem || appController.state.needsLogin || !appController.state.selectedUrl)
    readonly property string courseNoticeTitle: appController.state.busy ? appController.state.status
        : courseProblem ? "课程加载失败" : appController.state.needsLogin ? "请登录直播账号"
        : appController.state.courseLoadState === "empty" ? "今明暂无直播课程"
        : appController.courses.count > 0 ? "选择一节课程" : "读取你的直播课程"
    readonly property string courseNoticeHint: courseProblem ? appController.state.courseError
        : appController.state.needsLogin || appController.state.busy ? appController.state.courseHint
        : appController.courses.count > 0 ? "从左侧课程库选择课程，开始监控或预约。"
        : appController.state.courseLoadState === "empty" ? "可稍后刷新，也可在设置中填写课程网址。"
        : "读取今明课程；首次使用请先在设置中登录账号。"
    readonly property bool monitorEnabled: !appController.state.closing && !appController.state.stopping
        && (!appController.state.busy || appController.state.monitoring
            || (appController.state.accountMonitoring && appController.state.actionIcon === "clock"))
        && (appController.state.destructive || !appController.form.live || (!!appController.state.selectedUrl && !appController.state.needsLogin))
    onClosing: function(event) { event.accepted = accountManager ? accountManager.requestClose() : appController.requestClose() }
    Shortcut { sequence: "Ctrl+S"; onActivated: appController.save() }
    Shortcut { sequence: "Ctrl+Shift+L"; onActivated: appController.toggleTheme() }
    Shortcut { sequence: "Ctrl+\\"; onActivated: window.collapsed = !window.collapsed }
    Connections {
        target: appController
        function onNavigateSettings(key) { window.settingsVisible = true; window.collapsed = false }
        function onNavigateCourses() { window.settingsVisible = false; window.collapsed = false; documentScroll.contentY = 0 }
    }

    Popup {
        id: robotDialog
        objectName: "robotConflictDialog"
        property string selectedIdentity: ""
        property string lastTicket: ""
        readonly property var promptData: accountManager ? accountManager.robotPrompt : ({visible: false, ticket: "", description: "", choices: []})
        visible: promptData.visible
        anchors.centerIn: Overlay.overlay
        width: Math.min(window.width - 60, 600)
        padding: 24; modal: true; focus: true
        closePolicy: Popup.NoAutoClose
        background: Rectangle { radius: 6; color: Theme.surface; border.color: Theme.border; border.width: 1 }
        Connections {
            target: accountManager
            function onRobotPromptChanged() {
                var next = accountManager.robotPrompt
                if (robotDialog.lastTicket !== next.ticket || !next.choices.some(item => item.identity === robotDialog.selectedIdentity))
                    robotDialog.selectedIdentity = ""
                robotDialog.lastTicket = next.ticket
            }
        }
        contentItem: Column {
            spacing: 16
            UiText { text: "请选择课程机器人"; font.pixelSize: 20; font.weight: Font.DemiBold }
            UiText { width: parent.width; text: robotDialog.promptData.description; wrapMode: Text.Wrap; font.pixelSize: 14; color: Theme.muted }
            ComboBox {
                id: robotChoiceBox
                objectName: "availableRobotBox"
                width: parent.width
                model: robotDialog.promptData.choices
                textRole: "name"
                currentIndex: model.findIndex(item => item.identity === robotDialog.selectedIdentity)
                displayText: currentIndex < 0 ? "请选择空闲机器人" : currentText
                enabled: count > 0
                onActivated: robotDialog.selectedIdentity = model[currentIndex].identity
            }
            UiText {
                width: parent.width
                text: robotChoiceBox.count > 0 ? "已排除监听时间冲突的机器人（含课前、课后各两分钟）；时间不重叠可复用。" : "没有可用于本课程且测试成功的机器人。请先添加并测试新机器人，或取消本次操作。"
                wrapMode: Text.Wrap; font.pixelSize: 13; color: Theme.amber
            }
            RowLayout {
                width: parent.width; spacing: 8
                UiButton { objectName: "robotSettingsButton"; text: "添加机器人"; onClicked: accountManager.openRobotSettings(robotDialog.promptData.ticket) }
                Item { Layout.fillWidth: true }
                UiButton { objectName: "cancelRobotChoiceButton"; text: robotDialog.promptData.cancelText || "暂不处理"; outline: true; onClicked: accountManager.cancelRobotChoice(robotDialog.promptData.ticket) }
                UiButton { objectName: "confirmRobotChoiceButton"; text: "确认选择"; primary: true; enabled: robotChoiceBox.currentIndex >= 0; onClicked: accountManager.confirmRobotChoice(robotDialog.promptData.ticket, robotDialog.selectedIdentity) }
            }
        }
    }

    Rectangle {
        id: sidebar
        width: window.collapsed ? 56 : 240
        anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.left: parent.left
        color: Theme.sidebar
        Column {
            id: navigation
            x: 8; y: 6; width: parent.width - 16; spacing: 2
            // Workspace identity stays separate from the sidebar control.
            Item {
                width: parent.width; height: window.collapsed ? 72 : 36
                Item {
                    id: workspaceIdentity
                    y: window.collapsed ? 36 : 0
                    width: window.collapsed ? parent.width : parent.width - 32; height: 36
                    Row {
                        x: window.collapsed ? (parent.width - 22) / 2 : 8
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 8
                        Rectangle {
                            width: 22; height: 22; radius: 4; color: Theme.selected
                            anchors.verticalCenter: parent.verticalCenter
                            UiText { anchors.centerIn: parent; text: appController.form.username.slice(0, 1) || "·"; font.pixelSize: 12; font.weight: Font.DemiBold; color: Theme.muted }
                        }
                        UiText {
                            visible: !window.collapsed; width: Math.min(implicitWidth, workspaceIdentity.width - 24)
                            anchors.verticalCenter: parent.verticalCenter
                            text: appController.form.username || "登录账号"
                            font.pixelSize: 14; font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                    }
                }
                UiButton {
                    x: window.collapsed ? (parent.width - width) / 2 : parent.width - width; y: 4
                    width: 28; height: 28
                    iconName: window.collapsed ? "expand" : "collapse"
                    tooltip: window.collapsed ? "展开侧栏 (Ctrl+\\)" : "收起侧栏 (Ctrl+\\)"
                    onClicked: window.collapsed = !window.collapsed
                }
            }
            Item { width: 1; height: 6 }
                UiText {
                    visible: !window.collapsed
                    width: parent.width; height: 26; leftPadding: 10
                    text: "工作区"; color: Theme.subtle
                    font.pixelSize: 12; font.weight: Font.Medium
            }
            UiButton {
                objectName: "navLive"
                width: parent.width; height: 34; text: window.collapsed ? "" : "直播"; iconName: "camera"
                fontPixelSize: 15
                selected: !window.settingsVisible; alignLeft: !window.collapsed
                ink: selected ? Theme.text : Theme.muted
                tooltip: window.collapsed ? "直播" : ""
                onClicked: window.settingsVisible = false
            }
            UiButton {
                objectName: "navSettings"
                width: parent.width; height: 34; text: window.collapsed ? "" : "设置"; iconName: "settings"
                fontPixelSize: 15
                selected: window.settingsVisible; alignLeft: !window.collapsed
                ink: selected ? Theme.text : Theme.muted
                tooltip: window.collapsed ? "设置" : ""
                onClicked: { window.settingsVisible = true; window.collapsed = false }
            }
            Column {
                visible: accountManager !== null && !window.collapsed
                width: parent.width; spacing: 4
                Row {
                    width: parent.width; spacing: 6
                    UiText { text: "账号任务 · 上限 5 个"; color: Theme.subtle; font.pixelSize: 11; anchors.verticalCenter: parent.verticalCenter }
                    UiButton { text: "+"; width: 28; height: 26; tooltip: "添加账号"; onClicked: { accountManager.activate(""); window.settingsVisible = true } }
                }
                ListView {
                    objectName: "accountTaskList"
                    width: parent.width; height: Math.min(contentHeight, 230)
                    clip: true
                    model: accountManager ? accountManager.accountRows : []
                    delegate: Item {
                        required property var modelData
                        width: ListView.view.width; height: 46
                        Rectangle { anchors.fill: parent; radius: 4; color: modelData.active ? Theme.selected : "transparent" }
                        Column {
                            x: 8; y: 5; width: parent.width - 42; spacing: 3
                            UiText { width: parent.width; text: modelData.name + " · " + modelData.status; elide: Text.ElideRight; font.pixelSize: 12 }
                            UiText { width: parent.width; text: modelData.course || modelData.username; elide: Text.ElideRight; color: Theme.muted; font.pixelSize: 11 }
                        }
                        MouseArea { anchors.fill: parent; onClicked: accountManager.activate(modelData.username) }
                        UiButton {
                            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                            width: 30; height: 30; iconName: "stop"; tooltip: "停止此账号"
                            visible: modelData.monitoring
                            onClicked: accountManager.stopAccount(modelData.username)
                        }
                    }
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                }
                UiButton { text: "停止全部监听"; outline: true; width: parent.width; height: 26; onClicked: accountManager.stopAll() }
            }
        }
        Item {
            anchors.top: navigation.bottom; anchors.topMargin: 18
            anchors.bottom: actions.top; anchors.bottomMargin: 12
            anchors.left: parent.left; anchors.right: parent.right
            anchors.leftMargin: 8; anchors.rightMargin: 8
            visible: !window.collapsed
            Column {
                id: coursesPanel
                anchors.fill: parent
                spacing: 2
                Item {
                    width: parent.width; height: 26
                    UiText { objectName: "courseLibraryHeading"; leftPadding: 10; text: "课程库"; color: Theme.subtle; font.pixelSize: 12; font.weight: Font.Medium; anchors.verticalCenter: parent.verticalCenter }
                    UiButton { iconName: "refresh"; width: 24; height: 24; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; tooltip: "刷新课程"; enabled: !appController.state.busy; onClicked: appController.refreshCourses() }
                }
                ListView {
                    id: courseList
                    objectName: "courseList"
                    width: parent.width
                    height: Math.min(contentHeight, Math.max(100, coursesPanel.height - 40))
                    clip: true; reuseItems: true; spacing: 2
                    model: appController.courses
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: ItemDelegate {
                        id: courseDelegate
                        required property var entry
                        readonly property bool current: appController.state.selectedUrl === entry.url
                        width: courseList.width
                        height: courseContent.implicitHeight + 12
                        leftPadding: 10; rightPadding: 8; topPadding: 6; bottomPadding: 6
                        enabled: !appController.state.busy || appController.state.accountMonitoring
                        hoverEnabled: true
                        onClicked: { appController.selectCourse(entry.url); window.settingsVisible = false; documentScroll.contentY = 0 }
                        background: Rectangle {
                            radius: 4
                            color: courseDelegate.current ? Theme.selected : courseDelegate.hovered ? Theme.hover : "transparent"
                        }
                        contentItem: Column {
                            id: courseContent
                            spacing: 0
                            RowLayout {
                                width: parent.width; spacing: 8
                                Icon {
                                    name: "page"; width: 16; height: 16
                                    Layout.minimumWidth: 16; Layout.maximumWidth: 16
                                    Layout.minimumHeight: 16; Layout.maximumHeight: 16
                                    ink: courseDelegate.current ? Theme.text : Theme.muted
                                }
                                UiText { Layout.fillWidth: true; Layout.minimumWidth: 0; text: courseDelegate.entry.title; elide: Text.ElideRight; font.pixelSize: 14; color: courseDelegate.current ? Theme.text : Theme.muted; font.weight: courseDelegate.current ? Font.Medium : Font.Normal }
                                UiText { text: courseDelegate.entry.time; font.family: Theme.mono; font.pixelSize: 11; color: Theme.subtle }
                            }
                            Row {
                                x: 24; width: parent.width - 24; spacing: 6
                                Rectangle { visible: courseDelegate.entry.active; width: 6; height: 6; radius: 3; color: Theme.green; anchors.verticalCenter: parent.verticalCenter }
                                UiText {
                                    width: parent.width - (courseDelegate.entry.active ? 12 : 0)
                                    text: [courseDelegate.entry.monitoring ? "监听中" : courseDelegate.entry.reserved ? "已预约" : "", courseDelegate.entry.summary, courseDelegate.entry.detail].filter(function(part) { return part.length > 0 }).join(" · ")
                                    font.pixelSize: 12; color: courseDelegate.entry.monitoring ? Theme.green : courseDelegate.entry.reserved ? Theme.amber : Theme.subtle; elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }
                Item { width: 1; height: 8 }
                UiText { id: courseHint; visible: appController.courses.count === 0; width: parent.width; leftPadding: 10; rightPadding: 6; text: appController.state.courseHint; wrapMode: Text.Wrap; color: Theme.muted; font.pixelSize: 12; lineHeight: 1.5 }
            }
        }
        Column {
            id: actions
            anchors.bottom: parent.bottom; anchors.bottomMargin: 16
            anchors.left: parent.left; anchors.right: parent.right; anchors.margins: window.collapsed ? 8 : 12
            spacing: 10
            UiText {
                width: parent.width; visible: !window.collapsed && text.length > 0
                text: appController.state.feedback; color: appController.state.feedbackError ? Theme.red : Theme.subtle
                font.pixelSize: 12; wrapMode: Text.Wrap
            }
        }
    }
    Rectangle { x: sidebar.width; width: 1; height: parent.height; color: Theme.border }

    Item {
        id: main
        anchors.left: sidebar.right; anchors.leftMargin: 1
        anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom
        Item {
            id: topbar
            height: 44; width: parent.width
            RowLayout {
                x: 12; width: parent.width - topActions.width - 36
                anchors.verticalCenter: parent.verticalCenter; spacing: 0
                UiButton {
                    visible: !window.settingsVisible
                    text: "直播"; iconName: "camera"; height: 28
                    onClicked: documentScroll.contentY = 0
                }
                UiText { visible: !window.settingsVisible; text: "/"; color: Theme.subtle; width: 14; horizontalAlignment: Text.AlignHCenter; height: 28 }
                UiButton {
                    objectName: "pageBreadcrumb"
                    text: window.settingsVisible ? "设置" : window.coursePageTitle
                    iconName: window.settingsVisible ? "settings" : "page"
                    Layout.maximumWidth: Math.max(80, parent.width - 94)
                    tooltip: text
                    ink: Theme.text
                    height: 28
                    onClicked: {
                        if (window.settingsVisible) settingsPage.contentY = 0
                        else documentScroll.contentY = 0
                    }
                }
                Item { Layout.fillWidth: true }
            }
            Row {
                id: topActions
                anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
                spacing: 2
                UiButton {
                    objectName: "ongoingAction"
                    visible: appController.state.destructive && (window.settingsVisible || documentScroll.contentY > 120)
                    text: appController.state.actionText; iconName: appController.state.actionIcon
                    danger: true; height: 28; enabled: window.monitorEnabled
                    onClicked: appController.toggleMonitor()
                }
                UiButton { objectName: "focusButton"; width: 28; height: 28; iconName: "focus"; tooltip: "聚焦页面"; onClicked: window.collapsed = !window.collapsed }
                UiButton { objectName: "activityButton"; width: 28; height: 28; iconName: "history"; tooltip: "活动记录"; onClicked: { window.settingsVisible = false; window.activityExpanded = true; Qt.callLater(function() { documentScroll.contentY = Math.min(activityToggle.y, Math.max(0, documentScroll.contentHeight - documentScroll.height)) }) } }
                UiButton {
                    id: moreButton
                    width: 28; height: 28; iconName: "more"; tooltip: "更多选项"; onClicked: moreMenu.open()
                    Popup {
                        id: moreMenu
                        x: moreButton.width - width; y: moreButton.height + 4; width: 200; padding: 4
                        background: Rectangle { color: Theme.surface; radius: 4; border.width: 1; border.color: Theme.border }
                        contentItem: Column {
                            UiButton { width: parent.width; text: window.collapsed ? "退出聚焦" : "聚焦页面"; alignLeft: true; iconName: "focus"; onClicked: { moreMenu.close(); window.collapsed = !window.collapsed } }
                            UiButton { width: parent.width; text: appController.state.dark ? "切换浅色外观" : "切换深色外观"; alignLeft: true; iconName: appController.state.dark ? "sun" : "moon"; onClicked: { moreMenu.close(); appController.toggleTheme() } }
                            UiButton { width: parent.width; text: "打开设置"; alignLeft: true; iconName: "settings"; onClicked: { moreMenu.close(); window.settingsVisible = true; window.collapsed = false } }
                        }
                    }
                }
            }
        }
        Flickable {
            id: documentScroll
            objectName: "documentScroll"
            anchors.top: topbar.bottom; anchors.bottom: parent.bottom; width: parent.width
            visible: !window.settingsVisible
            clip: true; contentWidth: width; contentHeight: document.height + 72
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            Column {
                id: document
                objectName: "document"
                x: (parent.width - width) / 2; y: 24
                width: window.pageWidth
                spacing: 0
                RowLayout {
                    width: parent.width; spacing: 24
                    Column {
                        Layout.fillWidth: true; spacing: 6
                        UiText {
                            objectName: "pageTitle"
                            width: parent.width; text: window.coursePageTitle
                            font.family: Theme.heading; font.pixelSize: 32; font.weight: Font.Bold
                            lineHeight: 1.2; wrapMode: Text.Wrap
                        }
                        UiText {
                            objectName: "pageSubtitle"
                            width: parent.width
                            text: !appController.form.live ? "转写电脑正在播放的课堂声音"
                                : appController.state.courseSubtitle || (appController.state.selectedUrl ? "使用设置中的课程网址" : "课程、实时转写与签到提醒")
                            color: Theme.muted; font.pixelSize: 13; wrapMode: Text.Wrap
                        }
                    }
                    UiButton {
                        objectName: "monitorButton"
                        Layout.alignment: Qt.AlignTop; Layout.topMargin: 4
                        height: 36; text: appController.state.actionText
                        iconName: appController.state.actionIcon
                        danger: appController.state.destructive; primary: !danger
                        enabled: window.monitorEnabled
                        tooltip: !enabled && !appController.state.busy ? "请先登录并选择课程，或在设置中切换声音来源" : ""
                        onClicked: appController.toggleMonitor()
                    }
                }
                Item { width: 1; height: 16 }
                Property {
                    objectName: "statusRow"
                    iconName: "status"; name: "状态"
                    Rectangle {
                        height: 22; width: statusText.implicitWidth + 16; radius: 4; anchors.verticalCenter: parent.verticalCenter
                        color: appController.state.statusKind === "active" ? Theme.greenFill : appController.state.statusKind === "error" ? Theme.redFill : appController.state.statusKind === "waiting" ? Theme.amberFill : Theme.inset
                        Row {
                            id: statusText; anchors.centerIn: parent; spacing: 6
                            readonly property color tone: appController.state.statusKind === "active" ? Theme.green : appController.state.statusKind === "error" ? Theme.red : appController.state.statusKind === "waiting" ? Theme.amber : Theme.muted
                            Rectangle { width: 6; height: 6; radius: 3; color: statusText.tone; anchors.verticalCenter: parent.verticalCenter }
                            UiText { text: appController.state.status; font.pixelSize: 12; color: statusText.tone }
                        }
                    }
                }
                Property {
                    iconName: "clock"; name: appController.state.timerLabel
                    UiText { id: timerText; text: appController.state.elapsed; anchors.verticalCenter: parent.verticalCenter; font.family: Theme.mono; font.pixelSize: 13; color: Theme.text }
                }
                Property {
                    iconName: "camera"; name: "音频来源"
                    UiText { text: appController.form.live ? "后台直播音频" : "电脑声音"; anchors.verticalCenter: parent.verticalCenter; font.pixelSize: 14; color: Theme.text }
                }
                Item { width: 1; height: 12 }
                Rectangle { width: parent.width; height: 1; color: Theme.border }
                Item { width: 1; height: 20 }
                Column {
                    objectName: "reservationPanel"
                    width: parent.width; spacing: 8
                    visible: appController.state.reservationCount > 0
                    UiText { text: "本账号预约 · " + appController.state.reservationCount + " 门"; font.pixelSize: 15; font.weight: Font.DemiBold }
                    Repeater {
                        model: appController.reservations
                        delegate: RowLayout {
                            required property var modelData
                            width: parent.width; spacing: 8
                            Column {
                                Layout.fillWidth: true; spacing: 3
                                UiText { width: parent.width; text: modelData.title; elide: Text.ElideRight; font.pixelSize: 13 }
                                UiText { text: modelData.fullTime; color: Theme.muted; font.pixelSize: 12 }
                                UiText { width: parent.width; text: "机器人：" + (modelData.robotName || "未绑定"); elide: Text.ElideRight; color: modelData.robotPending ? Theme.amber : Theme.muted; font.pixelSize: 12 }
                            }
                            UiButton { text: "选择机器人"; height: 28; visible: accountManager !== null && (modelData.robotPending || modelData.robotName === "仅本地提醒"); onClicked: accountManager.chooseReservationRobot(appController.form.username, modelData.url) }
                            UiButton { text: "查看"; height: 28; enabled: !appController.state.busy || appController.state.accountMonitoring; onClicked: appController.selectCourse(modelData.url) }
                            UiButton { text: "取消"; height: 28; danger: true; enabled: !appController.state.closing; onClicked: appController.cancelReservation(modelData.url) }
                        }
                    }
                    UiText { width: parent.width; text: "到点自动依次监听，请保持软件运行。查看其他课程不影响预约。"; wrapMode: Text.Wrap; color: Theme.subtle; font.pixelSize: 12 }
                    Item { width: 1; height: 12 }
                }
                UiButton {
                    objectName: "returnToMonitor"
                    visible: appController.state.accountMonitoring && !appController.state.monitoring
                    text: "正在监听：" + appController.state.runningTitle + " · 点击返回"
                    iconName: "camera"; width: parent.width; alignLeft: true
                    onClicked: appController.selectCourse(appController.state.runningUrl)
                }
                UiButton {
                    objectName: "runningRobotButton"
                    visible: accountManager !== null && appController.state.accountMonitoring
                    text: "正在监听的课程机器人：" + appController.state.runningRobotName + (appController.state.runningRobotPending ? " · 点击选择" : "")
                    width: parent.width; alignLeft: true; enabled: appController.state.runningRobotPending
                    onClicked: accountManager.chooseReservationRobot(appController.form.username, appController.state.runningUrl)
                }
                Column {
                    objectName: "courseNotice"
                    visible: window.showCourseNotice
                    width: parent.width; spacing: 8
                    UiText { objectName: "courseNoticeTitle"; text: window.courseNoticeTitle; font.pixelSize: 18; font.weight: Font.DemiBold }
                    UiText { width: parent.width; text: window.courseNoticeHint; color: window.courseProblem ? Theme.red : Theme.muted; wrapMode: Text.Wrap }
                    Row {
                        spacing: 8
                        UiButton {
                            objectName: "retryCoursesButton"
                            visible: !appController.state.needsLogin && appController.courses.count === 0
                            text: window.courseProblem ? "重试读取" : "刷新课程"; iconName: "refresh"; outline: true
                            enabled: !appController.state.busy && !appController.state.closing
                            onClicked: appController.refreshCourses()
                        }
                        UiButton {
                            text: "账号与课程设置"; iconName: "settings"
                            onClicked: { window.settingsVisible = true; settingsPage.contentY = 0 }
                        }
                    }
                    Item { width: 1; height: 16 }
                }
                UiText { text: "实时转写"; font.family: Theme.heading; font.pixelSize: 20; font.weight: Font.DemiBold; lineHeight: 1.3 }
                Item { width: 1; height: 10 }
                Item {
                    objectName: "quoteBlock"
                    visible: !window.showCourseNotice || appController.state.transcriptFinal
                    width: parent.width; height: Math.max(28, transcript.contentHeight) + 8
                    Rectangle { width: 3; height: parent.height; color: Theme.text; radius: 0 }
                    TextEdit {
                        id: transcript
                        objectName: "transcript"
                        x: 17; y: 4; width: parent.width - 17; height: contentHeight
                        // Escape ASR text before applying paragraph spacing; it
                        // remains selectable and never interprets spoken markup.
                        text: "<p style='line-height:160%; margin:0;'>" + appController.state.transcript.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br>") + "</p>"
                        textFormat: TextEdit.RichText
                        color: appController.state.transcriptFinal ? Theme.text : Theme.muted
                        font.family: Theme.body; font.pixelSize: 17
                        renderType: Text.QtRendering
                        readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                        selectionColor: Theme.accentFill; selectedTextColor: Theme.text
                    }
                }
                UiText { visible: (!window.showCourseNotice || appController.state.transcriptFinal) && text.length > 0; width: parent.width - 17; x: 17; topPadding: 8; text: appController.state.transcriptHint; color: Theme.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
                UiText { visible: window.showCourseNotice && !appController.state.transcriptFinal; width: parent.width; text: "开始监控后，课堂内容将实时显示在这里。"; color: Theme.muted; font.pixelSize: 14; wrapMode: Text.Wrap }
                Item { width: 1; height: 24 }
                Rectangle {
                    id: codeCard
                    objectName: "codeCard"
                    readonly property bool hasCode: appController.state.code.length === 4
                    width: parent.width; height: 64; radius: 4
                    color: hasCode ? Theme.callout : Theme.inset
                    Row {
                        anchors.left: parent.left; anchors.leftMargin: 16; anchors.verticalCenter: parent.verticalCenter
                        spacing: 12
                        Icon { name: "key"; width: 18; ink: codeCard.hasCode ? Theme.calloutInk : Theme.muted; anchors.verticalCenter: parent.verticalCenter }
                        UiText { text: "签到码"; color: codeCard.hasCode ? Theme.calloutInk : Theme.muted; font.pixelSize: 14; anchors.verticalCenter: parent.verticalCenter }
                        UiText {
                            objectName: "codeValue"
                            text: codeCard.hasCode ? appController.state.code : "等待识别"
                            font.family: codeCard.hasCode ? Theme.mono : Theme.body
                            font.pixelSize: codeCard.hasCode ? 28 : 14
                            font.letterSpacing: codeCard.hasCode ? 3 : 0
                            font.weight: codeCard.hasCode ? Font.DemiBold : Font.Normal
                            color: codeCard.hasCode ? Theme.text : Theme.muted
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    UiButton {
                        objectName: "copyButton"
                        anchors.right: parent.right; anchors.rightMargin: 16; anchors.verticalCenter: parent.verticalCenter
                        text: appController.state.copied ? "已复制" : "复制"; iconName: appController.state.copied ? "check" : "copy"
                        ink: codeCard.hasCode ? Theme.calloutInk : Theme.muted
                        enabled: appController.state.code.length === 4; onClicked: appController.copyCode()
                    }
                }
                Item { width: 1; height: 28 }
                UiButton {
                    id: activityToggle
                    objectName: "activityToggle"
                    width: parent.width; height: 34; alignLeft: true
                    onClicked: window.activityExpanded = !window.activityExpanded
                    contentItem: Row {
                        leftPadding: 6; spacing: 8
                        Icon { name: "triangle"; width: 12; ink: Theme.text; rotation: window.activityExpanded ? 90 : 0; anchors.verticalCenter: parent.verticalCenter; Behavior on rotation { NumberAnimation { duration: 100 } } }
                        UiText { text: "活动记录"; color: Theme.text; font.pixelSize: 15; font.weight: Font.Medium; anchors.verticalCenter: parent.verticalCenter }
                        UiText { visible: appController.activity.count > 0; text: appController.activity.count + " 条"; color: Theme.subtle; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                    }
                }
                Loader {
                    id: activityContent
                    active: window.activityExpanded
                    width: parent.width
                    sourceComponent: Column {
                    width: parent.width; spacing: 12
                    Item { width: 1; height: 4 }
                    UiText { visible: appController.activity.count === 0; text: "暂无签到记录"; color: Theme.subtle; x: 26 }
                    ListView {
                        id: activityList
                        x: 26; width: parent.width - 26; height: Math.min(contentHeight, 240)
                        model: appController.activity
                        clip: true; reuseItems: true; spacing: 8
                        onCountChanged: if (window.followLogs) Qt.callLater(positionViewAtEnd)
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: Rectangle {
                            required property var entry
                            width: activityList.width; height: activityBody.height + 20; radius: 4
                            color: "transparent"; border.width: 1; border.color: Theme.border
                            Column {
                                id: activityBody; x: 12; y: 10; width: parent.width - 24; spacing: 6
                                UiText { text: entry.time; font.family: Theme.mono; font.pixelSize: 11; color: Theme.subtle }
                                UiText { width: parent.width; text: entry.text; wrapMode: Text.Wrap; color: entry.kind === "error" ? Theme.red : Theme.text }
                            }
                        }
                    }
                    Row {
                        anchors.right: parent.right; spacing: 12
                        Row {
                            height: 24; spacing: 8
                            anchors.verticalCenter: parent.verticalCenter
                            UiText { text: "自动滚动"; color: Theme.muted; font.pixelSize: 13; height: 24; verticalAlignment: Text.AlignVCenter }
                            AbstractButton {
                                id: followToggle
                                objectName: "followToggle"
                                width: 30; height: 18; checkable: true; checked: window.followLogs
                                anchors.verticalCenter: parent.verticalCenter
                                hoverEnabled: true; Accessible.name: "自动滚动"
                                onToggled: window.followLogs = checked
                                background: Rectangle {
                                    radius: 9
                                    color: followToggle.checked ? Theme.accent : followToggle.hovered ? Theme.outline : Theme.border
                                    Behavior on color { ColorAnimation { duration: 90 } }
                                    Rectangle {
                                        x: followToggle.checked ? 14 : 2; y: 2; width: 14; height: 14; radius: 7
                                        color: "#ffffff"
                                        Behavior on x { NumberAnimation { duration: 90 } }
                                    }
                                }
                            }
                        }
                        UiButton { text: "清空"; outline: true; onClicked: appController.clearActivity() }
                    }
                    ListView {
                        id: logList
                        objectName: "logList"
                        x: 26; width: parent.width - 26; height: 240
                        model: appController.logs
                        clip: true; reuseItems: true
                        onCountChanged: if (window.followLogs) Qt.callLater(positionViewAtEnd)
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: Item {
                            required property var entry
                            width: logList.width; height: Math.max(20, logText.implicitHeight) + 12
                            UiText { id: logTime; x: 4; y: 6; text: entry.time; font.family: Theme.mono; font.pixelSize: 11; color: Theme.subtle }
                            UiText { id: logText; x: 76; y: 6; width: parent.width - x - 8; text: entry.text; wrapMode: Text.Wrap; color: entry.kind === "error" ? Theme.red : entry.kind === "warning" ? Theme.amber : Theme.muted }
                        }
                        UiText { visible: appController.logs.count === 0; anchors.centerIn: parent; text: "监控开始后，活动记录会显示在这里"; color: Theme.subtle }
                    }
                    }
                }
            }
        }
        SettingsPanel {
            id: settingsPage
            objectName: "settingsPage"
            documentWidth: window.pageWidth
            anchors.top: topbar.bottom; anchors.bottom: parent.bottom
            width: parent.width
            visible: window.settingsVisible
        }
    }
}
