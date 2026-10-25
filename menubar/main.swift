// FxInternetBar —— fx-gateway 菜单栏入口
// 单色模板雷达图标(代码矢量绘制);面板尺寸由页面经 panelSize 消息实时上报,
// 绿态 400×~208、异常态 640×~304,详情展开更大——popover 始终贴合内容。
// 它只是查看器:崩溃/退出不影响任何网络功能(内核与守护都在 launchd/桥接里)。
import Cocoa
import WebKit
import ServiceManagement

let BRIDGE = "http://127.0.0.1:7900"

final class PanelController: NSObject, WKScriptMessageHandler {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let popover = NSPopover()
    let web = WKWebView()

    // 矢量绘制雷达图标(双环 + 中心点 + 右上波束);2x 位图,模板渲染随系统外观黑白
    func radarImage() -> NSImage {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 48, pixelsHigh: 48,
                                   bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                   isPlanar: false, colorSpaceName: .deviceRGB,
                                   bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = NSSize(width: 24, height: 24)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.black.setStroke()
        NSColor.black.setFill()
        let ring1 = NSBezierPath(ovalIn: NSRect(x: 2, y: 2, width: 20, height: 20))
        ring1.lineWidth = 2; ring1.stroke()
        let ring2 = NSBezierPath(ovalIn: NSRect(x: 5.6, y: 5.6, width: 12.8, height: 12.8))
        ring2.lineWidth = 2; ring2.stroke()
        NSBezierPath(ovalIn: NSRect(x: 9.9, y: 9.9, width: 4.2, height: 4.2)).fill()
        let beam = NSBezierPath()
        beam.move(to: NSPoint(x: 13.2, y: 10.8))
        beam.line(to: NSPoint(x: 19, y: 5))
        beam.lineWidth = 2; beam.lineCapStyle = .round; beam.stroke()
        NSGraphicsContext.restoreGraphicsState()
        let img = NSImage()
        img.addRepresentation(rep)
        img.isTemplate = true
        img.size = NSSize(width: 18, height: 18)
        return img
    }

    func start() {
        statusItem.button?.image = radarImage()
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePopover)

        // 页面常驻(打开即最新,不重载避免闪白与尺寸跳变)
        web.configuration.userContentController.add(self, name: "panelSize")
        web.setValue(false, forKey: "drawsBackground")  // 透明贴合气泡
        web.load(URLRequest(url: URL(string: BRIDGE + "/")!))

        let vc = NSViewController()
        vc.view = web
        popover.contentViewController = vc
        popover.behavior = .transient
        popover.contentSize = NSSize(width: 410, height: 240)  // 首帧按紧凑宽度猜,页面加载后自动校正

        // 幂等注册登录自启(替代 Verge 的开机自启角色)
        if #available(macOS 13.0, *) {
            try? SMAppService.mainApp.register()
        }
    }

    // 页面上报 {w, h} → popover 贴合内容
    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let w = body["w"] as? Int, let h = body["h"] as? Int else { return }
        let size = NSSize(width: max(320, min(820, w)),
                          height: max(140, min(700, h)))
        DispatchQueue.main.async {
            self.popover.contentSize = size
        }
    }

    @objc func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        if let button = statusItem.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // 无 Dock 图标,只活在菜单栏
let panel = PanelController()
panel.start()
app.run()
