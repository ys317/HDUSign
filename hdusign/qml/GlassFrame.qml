import QtQuick

// Lightweight light-catching edges over Windows' native desktop material.
Item {
    id: frame
    property real cornerRadius: 12
    visible: appearance.active
    clip: true
    readonly property real strength: appearance.values.highlight / 100
    Rectangle {
        anchors.fill: parent
        radius: frame.cornerRadius
        color: "transparent"
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, frame.strength * (Theme.dark ? 0.28 : 0.7))
    }
    Rectangle {
        x: 1; y: 1; width: parent.width - 2; height: Math.min(44, parent.height * 0.35)
        radius: frame.cornerRadius
        gradient: Gradient {
            GradientStop { position: 0; color: Qt.rgba(1, 1, 1, frame.strength * 0.12) }
            GradientStop { position: 1; color: "transparent" }
        }
    }
    HoverHandler { id: hover }
    Rectangle {
        x: Math.max(8, Math.min(parent.width - width - 8, hover.point.position.x - width / 2))
        y: 1; width: Math.min(110, parent.width / 2); height: 2
        opacity: hover.hovered ? frame.strength : 0
        Behavior on opacity { NumberAnimation { duration: 140 } }
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: "transparent" }
            GradientStop { position: 0.5; color: "#e8f6ff" }
            GradientStop { position: 1; color: "transparent" }
        }
    }
}
