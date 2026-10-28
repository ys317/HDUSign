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
        Toggle { caption: "后台直播音频"; description: "选择课程后直接接收直播声音，关闭时使用电脑声音。"; fieldKey: "live" }
        Field { viewport: panel; caption: "课程网址（选填）"; fieldKey: "url"; hint: "选课后自动填写，也可粘贴课程链接。" }
        UiButton { text: "打开课程页面 ↗"; outline: true; onClicked: appController.openCourse() }

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
        Heading { text: "全局玻璃外观" }
        Switch {
            id: glassSwitch
            objectName: "glassEnabledSwitch"
            text: "桌面透视玻璃"
            checked: appearance.values.enabled
            onToggled: appearance.setValue("enabled", checked)
            palette.windowText: Theme.text
            indicator: Rectangle {
                implicitWidth: 32; implicitHeight: 18
                x: glassSwitch.leftPadding; y: (glassSwitch.height - height) / 2
                radius: 9; color: glassSwitch.checked ? Theme.accent : Theme.outline
                Rectangle { x: glassSwitch.checked ? 16 : 2; y: 2; width: 14; height: 14; radius: 7; color: "#ffffff" }
            }
        }
        UiText { width: parent.width; text: appearance.status; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap }
        component GlassSlider: Column {
            property string caption
            property string settingKey
            width: parent.width; spacing: 3
            UiText { text: parent.caption + " · " + appearance.values[parent.settingKey] + "%"; color: Theme.text; font.pixelSize: 13 }
            Slider {
                id: glassSlider
                objectName: "glassSlider_" + parent.settingKey
                width: Math.min(420, parent.width)
                from: 0; to: 100; stepSize: 1
                value: appearance.values[parent.settingKey]
                enabled: appearance.values.enabled
                onMoved: appearance.setValue(parent.settingKey, value)
                background: Rectangle {
                    x: glassSlider.leftPadding; y: glassSlider.topPadding + glassSlider.availableHeight / 2 - height / 2
                    width: glassSlider.availableWidth; height: 4; radius: 2; color: Theme.outline
                    Rectangle { width: glassSlider.visualPosition * parent.width; height: parent.height; radius: 2; color: Theme.accent }
                }
                handle: Rectangle {
                    x: glassSlider.leftPadding + glassSlider.visualPosition * (glassSlider.availableWidth - width)
                    y: glassSlider.topPadding + glassSlider.availableHeight / 2 - height / 2
                    width: 16; height: 16; radius: 8; color: "#ffffff"
                    border.width: 2; border.color: glassSlider.pressed ? Theme.accentHover : Theme.accent
                }
            }
        }
        GlassSlider { caption: "背景透视程度"; settingKey: "transparency" }
        GlassSlider { caption: "玻璃着色浓度"; settingKey: "tint" }
        GlassSlider { caption: "液态边缘高光"; settingKey: "highlight" }
        UiText {
            width: parent.width; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap
            text: "即时预览，保存后对所有账号生效。系统负责桌面模糊，以上滑块调整透视、着色与高光；系统关闭透明效果时可能显示为实色。"
        }
        Row {
            spacing: 8
            UiButton { objectName: "saveGlassButton"; text: appearance.dirty ? "保存全局外观" : "外观已保存"; enabled: appearance.dirty; outline: true; onClicked: appearance.save() }
            UiButton { text: "恢复默认外观"; outline: true; onClicked: appearance.reset() }
        }
        UiText { width: parent.width; visible: appearance.feedback.length > 0; text: appearance.feedback; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap }

        Item { width: 1; height: 12 }
        UiText { text: appController.state.version; color: Theme.subtle; font.pixelSize: 11 }
    }
}
