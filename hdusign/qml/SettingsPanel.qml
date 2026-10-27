import QtQuick
import QtQuick.Controls

Flickable {
    id: panel
    property real documentWidth: Math.min(1120, width - 96)
    clip: true
    contentWidth: width
    contentHeight: fields.height + 72
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    // Notion settings section: heading with a hairline beneath it.
    component Heading: Column {
        property alias text: title.text
        width: parent.width; spacing: 8
        Item { width: 1; height: 20 }
        UiText { id: title; font.family: Theme.heading; font.pixelSize: 16; font.weight: Font.DemiBold }
        Rectangle { width: parent.width; height: 1; color: Theme.border }
        Item { width: 1; height: 2 }
    }

    Dialog {
        id: deleteAccountDialog
        property string account: ""
        title: "删除已保存账号"
        width: 420
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: Label { text: "删除账号 " + deleteAccountDialog.account + " 的密码和浏览器登录状态？"; wrapMode: Text.Wrap }
        onAccepted: appController.deleteAccount(account)
    }

    Column {
        id: fields
        objectName: "settingsDocument"
        x: (panel.width - width) / 2; y: 24
        width: panel.documentWidth; spacing: 12
        Column {
            width: parent.width; spacing: 6
            UiText { id: settingsHeading; objectName: "settingsHeading"; text: "设置"; font.family: Theme.heading; font.pixelSize: 32; font.weight: Font.Bold; lineHeight: 1.2 }
            UiText { width: parent.width; text: "账号、声音来源、签到辅助与识别规则"; color: Theme.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
        }
        Row {
            spacing: 8
            UiButton { outline: true; text: appController.state.dark ? "浅色外观" : "深色外观"; iconName: appController.state.dark ? "sun" : "moon"; onClicked: appController.toggleTheme() }
            UiButton { outline: true; text: appController.state.dirty ? "保存设置" : "已保存"; iconName: appController.state.dirty ? "" : "check"; enabled: appController.state.dirty; tooltip: "Ctrl+S"; onClicked: appController.save() }
        }

        Heading { text: "直播账号" }
        Row {
            spacing: 8
            ComboBox {
                id: savedAccountBox
                objectName: "savedAccountBox"
                width: Math.min(280, fields.width - 200)
                model: appController.savedAccounts
                currentIndex: -1
                displayText: currentIndex < 0 ? "选择已保存账号" : currentText
                enabled: accountManager !== null || !appController.state.busy
                onActivated: appController.selectAccount(currentText)
            }
            UiButton { text: "新账号"; outline: true; enabled: accountManager !== null || !appController.state.busy; onClicked: { savedAccountBox.currentIndex = -1; appController.selectAccount("") } }
            UiButton {
                text: "删除账号"; outline: true
                enabled: !appController.state.busy && savedAccountBox.currentIndex >= 0
                onClicked: { deleteAccountDialog.account = savedAccountBox.currentText; deleteAccountDialog.open() }
            }
        }
        UiText { width: parent.width; text: "选中账号后可修改学号或密码，登录成功后保存。修改学号会另存为新账号。"; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap }
        Toggle { caption: "记住此账号"; description: "登录成功后加密保存，下次自动填入。关闭时本次登录不更新已保存账号。"; fieldKey: "remember"; enabled: !appController.state.busy }
        Field { viewport: panel; caption: "学号 / 账号"; fieldKey: "username"; enabled: !appController.state.busy }
        Field { viewport: panel; caption: "密码"; fieldKey: "password"; secret: true; enabled: !appController.state.busy }
        Row {
            spacing: 8
            UiButton { text: "登录并读取课程"; primary: true; enabled: !appController.state.busy; onClicked: appController.login(false) }
            UiButton { text: "单独登录直播账号"; outline: true; enabled: !appController.state.busy; onClicked: appController.login(true) }
        }

        Heading { text: "声音来源" }
        Toggle { objectName: "liveAudioToggle"; visible: accountManager === null; caption: "后台直播音频"; description: "选择课程后直接接收直播声音，关闭时使用电脑声音。"; fieldKey: "live" }
        UiText {
            objectName: "liveAudioOnlyNotice"
            visible: accountManager !== null
            width: parent.width; wrapMode: Text.Wrap; color: Theme.muted; font.pixelSize: 13
            text: "后台直播音频：多账号界面直接接收各账号课程的直播声音。"
        }
        Field { viewport: panel; caption: "课程网址（选填）"; fieldKey: "url"; hint: "选课后自动填写，也可粘贴课程链接。" }
        UiButton { text: "打开课程页面 ↗"; outline: true; onClicked: appController.openCourse() }

        Heading { text: "签到辅助" }
        Toggle { objectName: "autoSignToggle"; caption: "自动签到"; description: "确认签到码后自动在当前账号的签到页面提交。"; fieldKey: "autoSign" }
        Row {
            width: parent.width; spacing: 12
            Field { width: (parent.width - 12) / 2; viewport: panel; caption: "纬度"; fieldKey: "latitude" }
            Field { width: (parent.width - 12) / 2; viewport: panel; caption: "经度"; fieldKey: "longitude" }
        }
        UiText { width: parent.width; text: appController.state.locationHint; wrapMode: Text.Wrap; color: Theme.muted; font.pixelSize: 13 }
        Row {
            spacing: 8
            UiButton { objectName: "locateButton"; text: "获取当前位置"; outline: true; enabled: !appController.state.busy; onClicked: appController.runAction("locate") }
            UiButton { objectName: "signinButton"; text: "重新登录签到"; outline: true; enabled: !appController.state.busy; onClicked: appController.runAction("signin") }
        }

        Heading { text: "识别与通知" }
        Toggle { caption: "语义辅助识别"; description: "使用本地语义模型辅助判断签到提示。"; fieldKey: "semantic" }
        Column {
            width: parent.width; spacing: 6
            UiText { text: "签到关键词（每行一项）"; color: Theme.muted; font.pixelSize: 13 }
            ScrollView {
                width: parent.width; height: 132
                TextArea {
                    objectName: "field_rules"
                    text: appController.form.rules
                    onTextChanged: if (activeFocus) appController.setField("rules", text)
                    color: Theme.text; font.family: Theme.body; font.pixelSize: 13
                    wrapMode: TextEdit.Wrap; selectByMouse: true
                    selectionColor: Theme.accentFill; selectedTextColor: Theme.text
                    background: Rectangle { color: Theme.inset; radius: 4; border.width: 1; border.color: parent.activeFocus ? Theme.accent : Theme.outline }
                }
            }
        }
        Heading { text: "诊断与日志" }
        Toggle {
            objectName: "diagnosticToggle"
            caption: "启用启动音频诊断"; fieldKey: "diagnostic"
            description: "保存开课前 2 分钟至开课后 1 分钟的音频；中途启动保存起始 3 分钟。下次监听生效。"
            enabled: !appController.state.busy
        }
        UiText {
            width: parent.width; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap
            text: "完整监听日志按账号始终保存。诊断开启时，另存音量、识别耗时、实时草稿和最多约 3 分钟 WAV 音频；低音量不等于没有人声。"
        }
        UiButton { objectName: "openLogsButton"; text: "打开本账号日志目录"; outline: true; onClicked: appController.openLogs() }

        Heading { text: "微信机器人" }
        Column {
            visible: accountManager !== null
            width: parent.width; spacing: 8
            UiText { text: "新增预约默认使用的机器人"; color: Theme.muted; font.pixelSize: 13 }
            Row {
                spacing: 8
                ComboBox {
                    id: webhookBox
                    objectName: "webhookBox"
                    width: Math.min(380, fields.width - 160)
                    model: accountManager ? accountManager.webhookChoices : []
                    textRole: "name"
                    currentIndex: accountManager ? Math.max(0, model.findIndex(item => item.identity === accountManager.webhookBinding)) : -1
                    enabled: !appController.state.busy
                    onActivated: accountManager.assignWebhook(model[currentIndex].identity)
                }
                UiButton {
                    text: "设为共用默认"; outline: true
                    enabled: !appController.state.busy && webhookBox.currentIndex > 0
                    onClicked: accountManager.setDefaultWebhook(webhookBox.model[webhookBox.currentIndex].identity)
                }
            }
            UiText { width: parent.width; text: "每门预约独立绑定机器人；更改此处不影响已有预约。机器人被预约或监听占用时，需要另选空闲机器人。新名称或链接测试成功才保存。"; wrapMode: Text.Wrap; color: Theme.muted; font.pixelSize: 12 }
            Field { viewport: panel; caption: "机器人名称"; fieldKey: "webhookName"; enabled: !appController.state.busy }
        }
        Field { viewport: panel; caption: "企业微信 Webhook"; fieldKey: "webhook"; secret: true; enabled: !appController.state.busy }
        UiButton { objectName: "testWebhookButton"; text: accountManager ? "测试推送并保存机器人" : "测试推送"; outline: true; enabled: !appController.state.busy; onClicked: appController.runAction("webhook") }
        Item { width: 1; height: 12 }
        UiText { text: appController.state.version; color: Theme.subtle; font.pixelSize: 11 }
    }
}
