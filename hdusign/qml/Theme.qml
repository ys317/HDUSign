pragma Singleton
import QtQuick

QtObject {
    readonly property bool dark: appController.state.dark
    readonly property color canvas: dark ? "#191919" : "#ffffff"
    readonly property color sidebar: dark ? "#202020" : "#f7f6f5"
    readonly property color text: dark ? "#e6e6e5" : "#37352f"
    readonly property color muted: dark ? "#a4a4a0" : "#787774"
    readonly property color subtle: dark ? "#94948f" : "#807f7b"
    readonly property color border: dark ? "#323232" : "#e9e9e7"
    readonly property color hover: dark ? "#2c2c2c" : "#efefed"
    readonly property color selected: dark ? "#303030" : "#eae9e7"
    readonly property color inset: dark ? "#272727" : "#f1f1ef"
    readonly property color surface: dark ? "#272727" : "#ffffff"
    readonly property color reserved: dark ? "#b310151d" : "#b3d8e1ea"
    readonly property color reservedSelected: dark ? "#d914202d" : "#d9c8d8e7"
    readonly property color red: dark ? "#f07878" : "#eb5757"
    readonly property color redFill: dark ? "#352727" : "#ffeceb"
    readonly property color redBorder: dark ? "#513434" : "#f4d5d5"
    readonly property color green: dark ? "#b3cfbf" : "#1c3829"
    readonly property color greenFill: dark ? "#29372d" : "#dbeddb"
    readonly property color amber: dark ? "#d4b972" : "#8f6b00"
    readonly property color amberFill: dark ? "#353021" : "#fbf3db"
    readonly property color callout: dark ? "#302b20" : "#fbf3db"
    readonly property color calloutInk: dark ? "#d4b972" : "#8f6b00"
    // Notion's single accent: primary actions, switches and text selection.
    readonly property color accent: "#2383e2"
    readonly property color accentHover: dark ? "#3a91e6" : "#0077d4"
    readonly property color accentFill: dark ? "#1f3a55" : "#d3e5f7"
    readonly property color outline: dark ? "#3a3a3a" : "#dfdfdc"
    readonly property string body: bodyFont
    readonly property string heading: headingFont
    readonly property string mono: monoFont
}
