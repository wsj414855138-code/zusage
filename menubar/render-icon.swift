// 渲染 ZUsage 菜单栏图标（bolt.fill）为 PNG，给用户预览实际外观
import AppKit

let size = NSSize(width: 256, height: 256)
let canvas = NSImage(size: size)
canvas.lockFocus()
NSColor.white.setFill()
NSRect(origin: .zero, size: size).fill()
if let symbol = NSImage(systemSymbolName: "bolt.fill", accessibilityDescription: "bolt") {
    let conf = NSImage.SymbolConfiguration(pointSize: 160, weight: .medium)
    let scaled = symbol.withSymbolConfiguration(conf) ?? symbol
    let s = scaled.size
    let origin = NSPoint(x: (size.width - s.width) / 2, y: (size.height - s.height) / 2)
    NSColor.black.set()
    scaled.draw(in: NSRect(origin: origin, size: s))
}
canvas.unlockFocus()

guard let tiff = canvas.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
let out = ("~/Desktop/Agent Team/zusage/docs/icon-preview.png" as NSString).expandingTildeInPath
try! png.write(to: URL(fileURLWithPath: out))
print("saved: \(out)")
