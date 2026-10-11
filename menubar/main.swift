// ZUsage — 多 Agent token 用量菜单栏应用（Popover 仪表盘版）
// 图标: 闪电+数字标题(可切换) · 点击弹出毛玻璃面板：
// 大数字头条 / 今日四格 / Agent+模型拆分(条形) / 模型速度(条形) / 每日柱状图
// 数据: 直读 ZCode sqlite(实时) + 统一库(60s launchd + 打开时补同步)
import AppKit
import SQLite3

let dbPath: String = ("~/.zcode/cli/db/db.sqlite" as NSString).expandingTildeInPath
let udbPath: String = ("~/.zcode/cli/agent-usage/usage.db" as NSString).expandingTildeInPath

/// 同步脚本路径（按优先级）：UserDefaults 覆盖 → 运行时部署副本 → 项目源文件。
/// 运行时副本优先：macOS TCC 保护 ~/Desktop，launchd 或 App 拉起的进程读不到桌面下的脚本。
func resolveSyncScript() -> String? {
    let override = UserDefaults.standard.string(forKey: "syncScriptPath")
    let candidates: [String?] = [
        override,
        ("~/.zcode/cli/agent-usage/sync.py" as NSString).expandingTildeInPath,
        ("~/Desktop/Agent Team/zusage/sync/sync.py" as NSString).expandingTildeInPath,
    ]
    for c in candidates {
        if let c = c, FileManager.default.fileExists(atPath: c) { return c }
    }
    return nil
}

let PANEL_W: CGFloat = 340
let CONTENT_W: CGFloat = PANEL_W - 32

func fmtCompact(_ v: Int64) -> String {
    let d = Double(v)
    if d >= 1_000_000 { return String(format: "%.1fM", d / 1_000_000) }
    if d >= 1_000 { return String(format: "%.0fk", d / 1_000) }
    return String(v)
}

func fmtSep(_ v: Int64) -> String {
    let nf = NumberFormatter()
    nf.numberStyle = .decimal
    return nf.string(from: NSNumber(value: v)) ?? String(v)
}

func colText(_ stmt: OpaquePointer?, _ i: Int32) -> String {
    guard let c = sqlite3_column_text(stmt, i) else { return "" }
    return String(cString: c)
}

func dispName(_ s: String) -> String {
    if s == "zcode" { return "ZCode" }
    if s == "codex" { return "Codex" }
    if s == "workbuddy" { return "WorkBuddy" }
    if s == "pi" { return "Pi" }
    if s == "kimi" { return "Kimi Code" }
    return s
}

func label(_ text: String, _ size: CGFloat, _ weight: NSFont.Weight = .regular,
           _ color: NSColor = .labelColor, mono: Bool = false) -> NSTextField {
    let font = mono ? NSFont.monospacedDigitSystemFont(ofSize: size, weight: weight)
                    : NSFont.systemFont(ofSize: size, weight: weight)
    let t = NSTextField(labelWithString: text)
    t.font = font
    t.textColor = color
    return t
}

func spacer() -> NSView {
    let v = NSView()
    v.setContentHuggingPriority(.init(1), for: .horizontal)
    v.setContentCompressionResistancePriority(.init(1), for: .horizontal)
    return v
}

func divider() -> NSBox {
    let b = NSBox()
    b.boxType = .separator
    b.widthAnchor.constraint(equalToConstant: CONTENT_W).isActive = true
    return b
}

func hstack(_ views: [NSView], spacing: CGFloat = 6, align: NSLayoutConstraint.Attribute = .centerY) -> NSStackView {
    let s = NSStackView(views: views)
    s.orientation = .horizontal
    s.spacing = spacing
    s.alignment = align
    s.widthAnchor.constraint(equalToConstant: CONTENT_W).isActive = true
    return s
}

func vstack(_ views: [NSView], spacing: CGFloat = 4) -> NSStackView {
    let s = NSStackView(views: views)
    s.orientation = .vertical
    s.spacing = spacing
    s.alignment = .leading
    return s
}

func clearStack(_ s: NSStackView) {
    for v in s.arrangedSubviews {
        s.removeArrangedSubview(v)
        v.removeFromSuperview()
    }
}

struct ModelSpeed {
    let name: String
    let tps: Double
    let n: Int
}

/// 单个模型在某个 Agent 下的用量与速度（tps = 0 表示该来源没有耗时可算）
struct ModelStat {
    let model: String
    let today: Int64
    let week: Int64
    let tps: Double
    let n: Int
}

struct AgentRow {
    let key: String
    let todayOut: Int64
    let weekOut: Int64
    let cost: Double
    let tps: Double        // 24h 汇总生成速度，0 = 无速度数据
    let speedN: Int
}

enum TitleMode: Int {
    case speedAndOut = 0   // 1 曾是费用模式已隐藏；旧存档值 1 会落回默认
    case speedOnly = 2

    var label: String {
        switch self {
        case .speedAndOut: return "速度+输出"
        case .speedOnly: return "仅速度"
        }
    }

    func next() -> TitleMode {
        return self == .speedAndOut ? .speedOnly : .speedAndOut
    }
}

struct Snapshot {
    var todayOut: Int64 = 0
    var todayIn: Int64 = 0
    var todayCache: Int64 = 0
    var todayReq = 0
    var weekOut: Int64 = 0
    var tps: Double = 0
    var speeds: [ModelSpeed] = []
    var agents: [AgentRow] = []
    var agentModels: [String: [ModelStat]] = [:]
    var daily: [(day: String, out: Int64)] = []
    var weekCost: Double = 0
}

// MARK: - 视图组件

final class BarView: NSView {
    private let fill = NSView()
    private var fillW: NSLayoutConstraint!
    private let trackW: CGFloat

    init(trackWidth: CGFloat, height: CGFloat = 5) {
        trackW = trackWidth
        super.init(frame: .zero)
        wantsLayer = true
        let track = NSView()
        track.wantsLayer = true
        track.layer?.backgroundColor = NSColor.separatorColor.cgColor
        track.layer?.cornerRadius = height / 2
        fill.wantsLayer = true
        fill.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        fill.layer?.cornerRadius = height / 2
        addSubview(track)
        track.addSubview(fill)
        track.translatesAutoresizingMaskIntoConstraints = false
        fill.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: trackW),
            heightAnchor.constraint(equalToConstant: height),
            track.leadingAnchor.constraint(equalTo: leadingAnchor),
            track.trailingAnchor.constraint(equalTo: trailingAnchor),
            track.topAnchor.constraint(equalTo: topAnchor),
            track.bottomAnchor.constraint(equalTo: bottomAnchor),
            fill.leadingAnchor.constraint(equalTo: track.leadingAnchor),
            fill.topAnchor.constraint(equalTo: track.topAnchor),
            fill.bottomAnchor.constraint(equalTo: track.bottomAnchor),
        ])
        fillW = fill.widthAnchor.constraint(equalToConstant: 0)
        fillW.isActive = true
    }

    required init?(coder: NSCoder) { return nil }

    func set(_ ratio: CGFloat) {
        fillW.constant = trackW * CGFloat(max(0, min(1, ratio)))
    }
}

final class DayColumn: NSView {
    private let bar = NSView()
    private var barH: NSLayoutConstraint!
    private let cap: NSTextField
    private let maxH: CGFloat = 30

    init() {
        cap = label("", 9, .regular, .secondaryLabelColor)
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.controlAccentColor.withAlphaComponent(0.6).cgColor
        bar.layer?.cornerRadius = 3
        cap.alignment = .center
        addSubview(bar)
        addSubview(cap)
        bar.translatesAutoresizingMaskIntoConstraints = false
        cap.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: 40),
            heightAnchor.constraint(equalToConstant: 46),
            bar.centerXAnchor.constraint(equalTo: centerXAnchor),
            bar.widthAnchor.constraint(equalToConstant: 22),
            bar.bottomAnchor.constraint(equalTo: cap.topAnchor, constant: -3),
            cap.centerXAnchor.constraint(equalTo: centerXAnchor),
            cap.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        barH = bar.heightAnchor.constraint(equalToConstant: 2)
        barH.isActive = true
    }

    required init?(coder: NSCoder) { return nil }

    func set(ratio: CGFloat, day: String, tip: String, highlight: Bool) {
        barH.constant = max(2, ratio * maxH)
        cap.stringValue = day
        bar.layer?.backgroundColor = (highlight ? NSColor.controlAccentColor
                                    : NSColor.controlAccentColor.withAlphaComponent(0.5)).cgColor
        toolTip = tip
    }
}

// MARK: - 面板

final class DashView: NSView {
    var onCycle: (() -> Void)?
    var onRefresh: (() -> Void)?
    var onViewToggle: (() -> Void)?
    private(set) var speedView = false

    private let main = NSStackView()
    private let speedBig = label("–", 21, .bold, .labelColor, mono: true)
    private let outBig = label("–", 21, .bold, .labelColor, mono: true)
    private var kv: [String: NSTextField] = [:]
    private let agentHeader = label("Agent · 今日 / 近 7 天", 11, .bold, .secondaryLabelColor)
    private let agentStack = vstack([], spacing: 7)
    private let speedStack = vstack([], spacing: 7)
    private let speedGroup = NSStackView()
    private var dayCols: [DayColumn] = []
    private let cycleBtn: NSButton
    private let viewBtn: NSButton

    override init(frame frameRect: NSRect) {
        cycleBtn = NSButton(title: "标题：速度+输出", target: nil, action: nil)
        viewBtn = NSButton(title: "视图：用量", target: nil, action: nil)
        super.init(frame: NSRect(x: 0, y: 0, width: PANEL_W, height: 380))
        wantsLayer = true

        let refreshBtn = NSButton(title: "刷新", target: nil, action: nil)
        let quitBtn = NSButton(title: "退出", target: nil, action: nil)
        for b in [cycleBtn, viewBtn, refreshBtn, quitBtn] {
            b.isBordered = false
            b.font = NSFont.systemFont(ofSize: 11)
            b.contentTintColor = .secondaryLabelColor
        }

        // 今日四格：fillEqually 等宽分栏。不给每格锁死宽度，否则总宽超出内容区会被压缩截断
        func kvBlock(_ cap: String) -> NSStackView {
            let v = label("–", 14, .semibold, .labelColor, mono: true)
            kv[cap] = v
            return vstack([v, label(cap, 10, .regular, .secondaryLabelColor)], spacing: 1)
        }
        let gridRow1 = hstack([kvBlock("7天输出"), kvBlock("今日输入"),
                               kvBlock("cache 读"), kvBlock("请求次数")], spacing: 6)
        gridRow1.distribution = .fillEqually

        // 每日柱状图
        let chartRow = hstack([], spacing: 4, align: .bottom)
        chartRow.distribution = .fillEqually
        for _ in 0..<7 {
            let c = DayColumn()
            dayCols.append(c)
            chartRow.addArrangedSubview(c)
        }

        cycleBtn.target = self
        cycleBtn.action = #selector(cycleTapped)
        viewBtn.target = self
        viewBtn.action = #selector(viewTapped)
        refreshBtn.target = self
        refreshBtn.action = #selector(refreshTapped)
        quitBtn.target = NSApp
        quitBtn.action = #selector(NSApplication.terminate(_:))

        let footer = hstack([cycleBtn, spacer(), viewBtn, refreshBtn, quitBtn], spacing: 8)

        main.orientation = .vertical
        main.alignment = .leading
        main.spacing = 8
        main.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 12, right: 16)
        main.translatesAutoresizingMaskIntoConstraints = false
        addSubview(main)
        // 只钉上/左/右：高度跟随内容自适应，杜绝被拉伸出大段空白
        NSLayoutConstraint.activate([
            main.topAnchor.constraint(equalTo: topAnchor),
            main.leadingAnchor.constraint(equalTo: leadingAnchor),
            main.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])

        let header = hstack([vstack([speedBig, label("tok/s · 生成速度 24h", 10, .regular, .secondaryLabelColor)], spacing: 1),
                             spacer(),
                             vstack([outBig, label("今日输出 tok", 10, .regular, .secondaryLabelColor)], spacing: 1)],
                            align: .centerY)
        speedBig.setContentHuggingPriority(.init(260), for: .horizontal)

        // 「模型速度」整块（含上方的分隔线）打包成一个组，便于在速度视图下整体折叠
        speedGroup.orientation = .vertical
        speedGroup.alignment = .leading
        speedGroup.spacing = 8
        speedGroup.addArrangedSubview(divider())
        speedGroup.addArrangedSubview(label("模型速度 · 近 24 小时", 11, .bold, .secondaryLabelColor))
        speedGroup.addArrangedSubview(speedStack)

        for v in [header, divider(), label("今日", 11, .bold, .secondaryLabelColor), gridRow1,
                  divider(), agentHeader, agentStack,
                  speedGroup,
                  divider(), label("每日输出 · 近 7 天", 11, .bold, .secondaryLabelColor), chartRow,
                  divider(), footer] {
            main.addArrangedSubview(v)
        }
    }

    required init?(coder: NSCoder) { return nil }

    @objc private func cycleTapped() { onCycle?() }
    @objc private func refreshTapped() { onRefresh?() }

    @objc private func viewTapped() {
        speedView.toggle()
        updateViewButton()
        onViewToggle?()
    }

    func setSpeedView(_ on: Bool) {
        speedView = on
        updateViewButton()
    }

    private func updateViewButton() {
        viewBtn.title = speedView ? "视图：速度" : "视图：用量"
    }

    func update(_ s: Snapshot) {
        speedBig.stringValue = s.tps > 0 ? String(format: "%.1f", s.tps) : "–"
        outBig.stringValue = fmtCompact(s.todayOut)
        kv["7天输出"]?.stringValue = fmtCompact(s.weekOut)
        kv["今日输入"]?.stringValue = fmtCompact(s.todayIn)
        kv["cache 读"]?.stringValue = fmtCompact(s.todayCache)
        kv["请求次数"]?.stringValue = String(s.todayReq)
        kv["7天输出"]?.toolTip = "\(fmtSep(s.weekOut)) tok"
        kv["今日输入"]?.toolTip = "\(fmtSep(s.todayIn)) tok"
        kv["cache 读"]?.toolTip = "\(fmtSep(s.todayCache)) tok"
        kv["请求次数"]?.toolTip = "今日 \(s.todayReq) 次模型请求"

        agentHeader.stringValue = speedView ? "Agent · 生成速度 24h" : "Agent · 今日 / 近 7 天"
        speedGroup.isHidden = speedView   // 速度视图下 Agent 区已含各模型速度，这块是重复的
        clearStack(agentStack)
        if speedView {
            // ZCode 固定在首位（头条数字就是它的），其余按速度排
            let rest = s.agents.filter { $0.key != "zcode" }.sorted { $0.tps > $1.tps }
            let ranked = s.agents.filter { $0.key == "zcode" } + rest
            let maxTps = ranked.first?.tps ?? 0
            for a in ranked where a.tps > 0 {
                let nm = dispName(a.key)
                let right = String(format: "%.1f t/s · %d 次", a.tps, a.speedN)
                agentStack.addArrangedSubview(vstack([
                    hstack([label(nm, 12, .medium), spacer(),
                            label(right, 11, .regular, .secondaryLabelColor, mono: true)]),
                    { let b = BarView(trackWidth: CONTENT_W); b.set(maxTps > 0 ? CGFloat(a.tps / maxTps) : 0); return b }(),
                ], spacing: 3))
                for m in (s.agentModels[a.key] ?? []).filter({ $0.tps > 0 }).prefix(3) {
                    agentStack.addArrangedSubview(hstack([
                        label("↳ \(m.model)", 10, .regular, .secondaryLabelColor), spacer(),
                        label(String(format: "%.1f t/s · %d次", m.tps, m.n), 10, .regular, .tertiaryLabelColor, mono: true)]))
                }
            }
            let noSpeed = ranked.filter { $0.tps <= 0 }
            if !noSpeed.isEmpty {
                let names = noSpeed.map { dispName($0.key) }.joined(separator: "、")
                agentStack.addArrangedSubview(label("24h 内无生成样本：\(names)", 11, .regular, .tertiaryLabelColor))
            }
        } else {
            let maxWeek = s.agents.map { $0.weekOut }.max() ?? 0
            // ZCode 固定在首位，其余按今日用量、再按 7 天用量排（避免并列时顺序随机）
            let rest = s.agents.filter { $0.key != "zcode" }
                .sorted { ($0.todayOut, $0.weekOut) > ($1.todayOut, $1.weekOut) }
            for a in s.agents.filter({ $0.key == "zcode" }) + rest {
                let nm = dispName(a.key)
                let right = a.todayOut > 0
                    ? "今日 \(fmtCompact(a.todayOut)) · 7天 \(fmtCompact(a.weekOut))"
                    : "7天 \(fmtCompact(a.weekOut))"
                agentStack.addArrangedSubview(vstack([
                    hstack([label(nm, 12, .medium), spacer(), label(right, 11, .regular, .secondaryLabelColor, mono: true)]),
                    { let b = BarView(trackWidth: CONTENT_W); b.set(maxWeek > 0 ? CGFloat(a.weekOut) / CGFloat(maxWeek) : 0); return b }(),
                ], spacing: 3))
                if let models = s.agentModels[a.key] {
                    for m in models.prefix(3) {
                        let t = m.today > 0 ? "今日 \(fmtCompact(m.today))" : "7天 \(fmtCompact(m.week))"
                        agentStack.addArrangedSubview(hstack([
                            label("↳ \(m.model)", 10, .regular, .secondaryLabelColor), spacer(),
                            label(t, 10, .regular, .tertiaryLabelColor, mono: true)]))
                    }
                }
            }
        }

        clearStack(speedStack)
        let maxTps = s.speeds.first?.tps ?? 0
        for m in s.speeds.prefix(5) {
            speedStack.addArrangedSubview(vstack([
                hstack([label(m.name, 12), spacer(),
                        label(String(format: "%.1f t/s · %d次", m.tps, m.n), 11, .regular, .secondaryLabelColor, mono: true)]),
                { let b = BarView(trackWidth: CONTENT_W); b.set(maxTps > 0 ? CGFloat(m.tps / maxTps) : 0); return b }(),
            ], spacing: 3))
        }
        if s.speeds.isEmpty {
            speedStack.addArrangedSubview(label("暂无数据", 11, .regular, .tertiaryLabelColor))
        }

        let maxDay = s.daily.map { $0.out }.max() ?? 0
        for (i, col) in dayCols.enumerated() {
            if i < s.daily.count {
                let d = s.daily[i]
                let isMax = maxDay > 0 && d.out == maxDay
                col.set(ratio: maxDay > 0 ? sqrt(CGFloat(d.out) / CGFloat(maxDay)) : 0,
                        day: String(d.day.suffix(2)),
                        tip: "\(d.day)  \(fmtCompact(d.out)) tok",
                        highlight: isMax)
                col.isHidden = false
            } else {
                col.isHidden = true
            }
        }
    }

    func setCycleTitle(_ t: String) {
        cycleBtn.title = "标题：\(t)"
    }

    /// 内容实测高度（用于 popover 自适应尺寸）。fittingSize 已包含 edgeInsets，不可重复相加。
    func contentHeight() -> CGFloat {
        layoutSubtreeIfNeeded()
        return max(180, main.fittingSize.height)
    }
}

final class DashVC: NSViewController {
    let dash: DashView
    init(dash: DashView) {
        self.dash = dash
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { return nil }
    override func loadView() { view = dash }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private let popover = NSPopover()
    private let dash = DashView()
    private var timer: Timer?
    private var ticks = 0
    private var lastSyncAttempt = Date.distantPast
    private var lastRenderKey = ""
    private var loggedMissingScript = false
    private var titleMode: TitleMode
    private var snap = Snapshot()

    override init() {
        titleMode = TitleMode(rawValue: UserDefaults.standard.integer(forKey: "titleMode")) ?? .speedAndOut
        super.init()
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        let vc = DashVC(dash: dash)
        popover.contentViewController = vc
        popover.behavior = .transient
        popover.contentSize = NSSize(width: PANEL_W, height: 380)

        dash.onCycle = { [weak self] in self?.cycleTitle() }
        dash.onRefresh = { [weak self] in self?.refreshNow() }
        dash.onViewToggle = { [weak self] in
            guard let self else { return }
            UserDefaults.standard.set(self.dash.speedView, forKey: "speedView")
            self.dash.update(self.snap)
            self.sizePopover()
        }
        dash.setSpeedView(UserDefaults.standard.bool(forKey: "speedView"))
        dash.update(snap)
        dash.setCycleTitle(titleMode.label)

        setupItem()
        refresh()
        // 调试开关：启动后自动弹出面板，便于截图验证 / 演示。
        // defaults write local.zusage.zusage debugAutoOpen -bool true
        if UserDefaults.standard.bool(forKey: "debugAutoOpen") {
            popover.behavior = .applicationDefined   // 调试模式下不因失焦自动关闭，便于截图
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                self?.togglePopover()
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.ticks += 1
            // 重建状态项会让图标在菜单栏里重新排序，默认关闭；需要「腾出位置后自动补位」时
            // 可执行：defaults write local.zusage.zusage autoReposition -bool true
            if self.ticks % 12 == 0 && UserDefaults.standard.bool(forKey: "autoReposition") {
                self.setupItem()
            }
            self.refresh()
        }
    }

    private func setupItem() {
        if let existing = item {
            NSStatusBar.system.removeStatusItem(existing)
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let img = NSImage(systemSymbolName: "bolt.fill", accessibilityDescription: "ZUsage") {
            item.button?.image = img
        }
        item.button?.action = #selector(togglePopover)
        item.button?.target = self
        updateTitle()
    }

    @objc private func togglePopover() {
        guard let button = item.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        dash.update(snap)
        dash.setCycleTitle(titleMode.label)
        sizePopover()
        lastRenderKey = renderKey(snap)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
    }

    private func sizePopover() {
        let contentH = dash.contentHeight()
        let avail = (NSScreen.main?.visibleFrame.height ?? 900) - 40
        let h = min(contentH, avail)
        popover.contentViewController?.preferredContentSize = NSSize(width: PANEL_W, height: h)
    }

    @objc private func refreshNow() { refresh() }

    @objc private func cycleTitle() {
        titleMode = titleMode.next()
        UserDefaults.standard.set(titleMode.rawValue, forKey: "titleMode")
        updateTitle()
        dash.setCycleTitle(titleMode.label)
    }

    /// 从统一库读取上次同步时间。WAL 模式下主库文件 mtime 不再随写入变化，不能作为依据。
    private func lastSyncMs() -> Double? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(udbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        sqlite3_busy_timeout(db, 1000)
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT state FROM sync_state WHERE adapter = '_meta'", -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            return nil
        }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW, let c = sqlite3_column_text(stmt, 0) else { return nil }
        let json = String(cString: c)
        guard let data = json.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: data),
              let obj = parsed as? [String: Any],
              let ms = obj["last_sync"] as? Double else { return nil }
        return ms
    }

    /// 数据过期时补跑一次同步（兜底 launchd 的 60 秒周期）。
    /// 主线程只做判断与派发，子进程和等待全部在后台队列，绝不阻塞 UI。
    private func syncIfStale() {
        guard Date().timeIntervalSince(lastSyncAttempt) > 30 else { return }  // 最小间隔，避免反复起进程
        let age: TimeInterval
        if let ms = lastSyncMs() {
            age = Date().timeIntervalSince1970 - ms / 1000.0
        } else {
            age = 9999
        }
        guard age > 45 else { return }
        guard let script = resolveSyncScript() else {
            if !loggedMissingScript {
                loggedMissingScript = true
                NSLog("ZUsage: 未找到同步脚本，暂不做兜底同步（可用 UserDefaults 键 syncScriptPath 指定）")
            }
            return
        }
        lastSyncAttempt = Date()
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            p.arguments = [script]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            do {
                try p.run()
            } catch {
                NSLog("ZUsage: 同步脚本启动失败 \(error)")
                return
            }
            let deadline = Date().addingTimeInterval(15)
            while p.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.2)
            }
            if p.isRunning { p.terminate() }
            DispatchQueue.main.async { self?.refresh() }   // 同步完成后立刻刷新一次
        }
    }

    private func refresh() {
        syncIfStale()
        var s = Snapshot()
        let cal = Calendar.current
        let startOfToday = Int64(cal.startOfDay(for: Date()).timeIntervalSince1970 * 1000)
        let weekAgo = startOfToday - 6 * 86_400_000
        let dayAgo = Int64(Date().timeIntervalSince1970 * 1000) - 86_400_000

        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return  // 保留 snap 原值，不闪空
        }
        sqlite3_busy_timeout(db, 2000)

        var stmt: OpaquePointer?
        let sql = "SELECT started_at, model_id, status, input_tokens, output_tokens, cache_read_input_tokens, first_token_at, completed_at FROM model_usage WHERE started_at >= ? ORDER BY started_at"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            sqlite3_close(db)
            return
        }
        sqlite3_bind_int64(stmt, 1, weekAgo)

        var mOut: [String: Int64] = [:]
        var mSec: [String: Double] = [:]
        var mN: [String: Int] = [:]
        var totalGenSec = 0.0
        var zcDaily: [String: Int64] = [:]
        var zcModelWeek: [String: Int64] = [:]
        var zcModelToday: [String: Int64] = [:]
        let df = DateFormatter()
        df.dateFormat = "MM-dd"

        while sqlite3_step(stmt) == SQLITE_ROW {
            let started = sqlite3_column_int64(stmt, 0)
            let model = colText(stmt, 1)
            let status = colText(stmt, 2)
            let input = sqlite3_column_int64(stmt, 3)
            let out = sqlite3_column_int64(stmt, 4)
            let cache = sqlite3_column_int64(stmt, 5)
            let ft = sqlite3_column_int64(stmt, 6)
            let ct = sqlite3_column_int64(stmt, 7)

            s.weekOut += out
            let dayKey = df.string(from: Date(timeIntervalSince1970: Double(started) / 1000))
            zcDaily[dayKey, default: 0] += out
            zcModelWeek[model, default: 0] += out
            if started >= startOfToday {
                s.todayReq += 1
                s.todayOut += out
                s.todayIn += input
                s.todayCache += cache
                zcModelToday[model, default: 0] += out
            }
            if status == "completed", ft > 0, ct > ft, started >= dayAgo {
                let gen = Double(ct - ft) / 1000.0
                if gen > 0.05 {
                    mOut[model, default: 0] += out
                    mSec[model, default: 0] += gen
                    mN[model, default: 0] += 1
                    totalGenSec += gen
                }
            }
        }
        sqlite3_finalize(stmt)
        sqlite3_close(db)

        if totalGenSec > 0 {
            s.tps = Double(mOut.values.reduce(0, +)) / totalGenSec
        }
        s.speeds = mOut.keys.map { m in
            ModelSpeed(name: m,
                       tps: (mSec[m] ?? 0) > 0 ? Double(mOut[m] ?? 0) / (mSec[m] ?? 1) : 0,
                       n: mN[m] ?? 0)
        }
        .sorted { $0.tps > $1.tps }

        loadUnified(into: &s, weekAgo: weekAgo, dayAgo: dayAgo)
        let zcSpeedN = mN.values.reduce(0, +)
        s.agents.insert(AgentRow(key: "zcode", todayOut: s.todayOut,
                                 weekOut: s.weekOut, cost: 0,
                                 tps: s.tps, speedN: zcSpeedN), at: 0)
        if !zcModelWeek.isEmpty {
            s.agentModels["zcode"] = zcModelWeek.keys.map { m -> ModelStat in
                let sec = mSec[m] ?? 0
                return ModelStat(model: m,
                                 today: zcModelToday[m] ?? 0,
                                 week: zcModelWeek[m] ?? 0,
                                 tps: sec > 0 ? Double(mOut[m] ?? 0) / sec : 0,
                                 n: mN[m] ?? 0)
            }.sorted { $0.week > $1.week }
        }
        var merged: [String: Int64] = [:]
        for d in s.daily { merged[d.day, default: 0] += d.out }
        for (k, v) in zcDaily { merged[k, default: 0] += v }
        s.daily = merged.map { (day: $0.key, out: $0.value) }.sorted { $0.day < $1.day }
        snap = s
        updateTitle()
        let key = renderKey(s)
        if key != lastRenderKey {
            lastRenderKey = key
            if popover.isShown {
                dash.update(s)
                sizePopover()
            }
        }
    }

    private func loadUnified(into s: inout Snapshot, weekAgo: Int64, dayAgo: Int64) {
        var db: OpaquePointer?
        guard sqlite3_open_v2(udbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return
        }
        sqlite3_busy_timeout(db, 2000)
        defer { sqlite3_close(db) }

        let startOfToday = Int64(Calendar.current.startOfDay(for: Date()).timeIntervalSince1970 * 1000)

        var stmt: OpaquePointer?
        let sql = "SELECT agent, model, ts, output_tokens, cost_usd, gen_ms FROM requests WHERE ts >= ? AND agent != 'zcode'"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            return
        }
        sqlite3_bind_int64(stmt, 1, weekAgo)

        var week: [String: (out: Int64, cost: Double)] = [:]
        var today: [String: Int64] = [:]
        var dayOut: [String: Int64] = [:]
        var mWeek: [String: [String: Int64]] = [:]
        var mToday: [String: [String: Int64]] = [:]
        var aGen: [String: (out: Int64, sec: Double, n: Int)] = [:]
        var mGen: [String: [String: (out: Int64, sec: Double, n: Int)]] = [:]
        let df = DateFormatter()
        df.dateFormat = "MM-dd"

        while sqlite3_step(stmt) == SQLITE_ROW {
            let a = colText(stmt, 0)
            let mdl = colText(stmt, 1)
            let ts = sqlite3_column_int64(stmt, 2)
            let out = sqlite3_column_int64(stmt, 3)
            let cost = sqlite3_column_type(stmt, 4) == SQLITE_NULL
                ? 0.0 : sqlite3_column_double(stmt, 4)
            let gen = sqlite3_column_type(stmt, 5) == SQLITE_NULL
                ? 0 : sqlite3_column_int64(stmt, 5)

            var w = week[a] ?? (0, 0)
            w.out += out
            w.cost += cost
            week[a] = w
            let isToday = ts >= startOfToday
            if isToday {
                today[a, default: 0] += out
            }
            let key = df.string(from: Date(timeIntervalSince1970: Double(ts) / 1000))
            dayOut[key, default: 0] += out
            if !mdl.isEmpty {
                mWeek[a, default: [:]][mdl, default: 0] += out
                if isToday {
                    mToday[a, default: [:]][mdl, default: 0] += out
                }
            }
            if gen > 50 && ts >= dayAgo {   // 速度统一 24h 口径，与 ZCode 直读部分一致
                var g = aGen[a] ?? (0, 0, 0)
                g.out += out; g.sec += Double(gen) / 1000.0; g.n += 1
                aGen[a] = g
                if !mdl.isEmpty {
                    var mg = mGen[a]?[mdl] ?? (0, 0, 0)
                    mg.out += out; mg.sec += Double(gen) / 1000.0; mg.n += 1
                    mGen[a, default: [:]][mdl] = mg
                }
            }
        }
        sqlite3_finalize(stmt)

        func tpsOf(_ g: (out: Int64, sec: Double, n: Int)?) -> (Double, Int) {
            guard let g = g, g.sec > 0, g.out > 0 else { return (0, 0) }
            return (Double(g.out) / g.sec, g.n)
        }

        for (a, dict) in mWeek {
            let arr = dict.keys.map { m -> ModelStat in
                let t = tpsOf(mGen[a]?[m])
                return ModelStat(model: m, today: mToday[a]?[m] ?? 0,
                                 week: dict[m] ?? 0, tps: t.0, n: t.1)
            }
            s.agentModels[a] = arr.sorted { $0.week > $1.week }
        }

        for (a, w) in week {
            let t = tpsOf(aGen[a])
            s.agents.append(AgentRow(key: a, todayOut: today[a] ?? 0,
                                     weekOut: w.out, cost: w.cost, tps: t.0, speedN: t.1))
            s.weekCost += w.cost
        }
        s.agents.sort { $0.todayOut > $1.todayOut }
        s.daily = dayOut.keys.map { (day: $0, out: dayOut[$0] ?? 0) }.sorted { $0.day < $1.day }
    }

    /// 面板展示内容的指纹：内容真的变了才重绘，避免每 5 秒重建整棵视图树造成闪烁。
    private func renderKey(_ s: Snapshot) -> String {
        var parts = [dash.speedView ? "S" : "U",
                     String(format: "%.0f", s.tps),
                     "\(s.todayOut)", "\(s.todayIn)", "\(s.todayCache)", "\(s.todayReq)",
                     "\(s.weekOut)"]
        for a in s.agents { parts.append("\(a.key):\(a.todayOut):\(a.weekOut):\(a.speedN)") }
        for m in s.speeds { parts.append("\(m.name):\(Int(m.tps * 10)):\(m.n)") }
        for d in s.daily { parts.append("\(d.day):\(d.out)") }
        for k in s.agentModels.keys.sorted() {
            for m in s.agentModels[k] ?? [] {
                parts.append("\(k)/\(m.model):\(m.today):\(m.week):\(m.n)")
            }
        }
        return parts.joined(separator: "|")
    }

    private func updateTitle() {
        let sp = snap.tps > 0 ? String(format: "%.0f", snap.tps) : "–"
        let out = fmtCompact(snap.todayOut)
        let text = titleMode == .speedAndOut ? "\(sp) · \(out)" : sp
        item.button?.title = text
        item.button?.toolTip = "ZCode ⚡\(sp) t/s · 今日输出 \(out) tok"
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
