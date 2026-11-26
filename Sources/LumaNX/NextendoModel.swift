import AppKit
import NextendoCore
import SwiftUI

/// Account state for optional Nextendo play. Tokens stay inside NextendoCore's
/// Keychain store; only the public client ID is kept in preferences.
@MainActor final class NextendoModel: ObservableObject {
    static let scopes = ["identity", "friends", "game.matchmaking", "presence"]
    @Published private(set) var account: NextendoAccountSummary?
    @Published private(set) var checkedAccount = false
    @Published private(set) var busy = false
    @Published private(set) var signingIn = false
    @Published var message: String?
    private let service: NextendoAccountService
    private let open: (URL) -> Bool
    private var task: Task<Void, Never>?

    init(service: NextendoAccountService = NextendoAccountService(), open: @escaping (URL) -> Bool = { NSWorkspace.shared.open($0) }) {
        self.service = service; self.open = open
    }
    static func validClientID(_ id: String) -> Bool {
        id.range(of: "^nxc_[A-Za-z0-9_-]{3,128}$", options: .regularExpression) != nil
    }
    /// A nil credential means there is no signed-in account. Renewal failures
    /// are reported to the caller so it can distinguish them from sign-out.
    func gameLaunchCredential() async throws -> Data? {
        guard !busy else { throw NextendoError.authorizationUnavailable }
        busy = true; defer { busy = false }
        do {
            account = try await service.currentAccount()
            checkedAccount = true
            message = nil
            guard account != nil else { return nil }
            account = try await service.prepareGameAccount()
            guard let account else { return nil }
            if account.isExpired { throw NextendoError.authorizationExpired }
            return try await service.gameLaunchCredential()
        } catch {
            checkedAccount = true
            message = Self.describe(error)
            throw error
        }
    }

    func refreshStatus() async {
        guard !busy else { return }
        do {
            let current = try await service.currentAccount()
            guard !busy else { return }
            account = current
            message = nil
        } catch {
            guard !busy else { return }
            message = Self.describe(error)
        }
        checkedAccount = true
    }

    func reload() async {
        guard !busy else { return }
        busy = true; defer { busy = false; checkedAccount = true }
        do {
            account = try await service.currentAccount()
            if account != nil { account = try await service.prepareGameAccount() }
            message = nil
        } catch { message = Self.describe(error) }
    }
    func signIn(clientID: String) {
        guard !busy else { return }
        let id = clientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.validClientID(id) else { message = "请输入在 Nextendo 开发者页面注册的客户端 ID（以 nxc_ 开头）。"; return }
        busy = true; signingIn = true; message = nil
        task = Task {
            defer { busy = false; signingIn = false; task = nil }
            do {
                let configuration = try NextendoOAuthConfiguration(clientID: id, scopes: Self.scopes)
                let request = try await service.beginSignIn(configuration: configuration)
                guard open(request.authorizationURL) else {
                    await service.cancelSignIn(); message = "无法打开登录页面，请检查默认浏览器。"; return
                }
                account = try await service.finishSignIn(request.id, forGames: true)
                checkedAccount = true
            } catch is CancellationError {} catch { message = Self.describe(error) }
        }
    }
    func cancel() {
        guard signingIn else { return }
        task?.cancel()
        Task { await service.cancelSignIn() }
    }
    func signOut() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do { try await service.signOut(); account = nil; checkedAccount = true; message = nil } catch { message = Self.describe(error) }
    }
    /// Transport errors can contain request URLs, so only typed errors are shown.
    static func describe(_ error: Error) -> String {
        (error as? NextendoError)?.errorDescription ?? "Nextendo 登录未完成，请重试。"
    }
}

struct NextendoPreparationStatus {
    let game: Game
    let catalog: NextendoCatalogSnapshot?

    var targetVersion: String? { row == nil ? nil : game.onlinePreparation?.version }
    var eligibility: NextendoGameEligibility {
        catalog?.evaluate(titleID:game.id,installedVersion:game.onlinePreparation?.version) ?? .catalogUnavailable
    }
    private var row: NextendoGamePolicy? {
        guard catalog?.freshness() == .fresh else { return nil }
        return catalog?.catalog?.games.first { $0.titleIDs.contains(game.id.lowercased()) }
    }
    var acceptedVersions: [String] {
        guard let row, case .exact(let versions) = row.versionPolicy else { return [] }
        return versions
    }
    private var installedRetailVersion: String? {
        [game.runtimeRetailVersion, game.runtimeVersion].compactMap { $0 }.first {
            $0.range(of:#"^\d+\.\d+(?:\.\d+){0,2}$"#,options:.regularExpression) != nil
        }
    }
    var installedVersion: String? {
        guard row != nil, game.ready == true else { return nil }
        return installedRetailVersion ?? game.runtimeVersionLabel
    }
    var installedVersionNotAccepted: Bool {
        guard game.ready == true, let installedRetailVersion, let row else { return false }
        if case .versionNotAccepted = row.evaluate(installedVersion:installedRetailVersion) { return true }
        return false
    }
    var showAcceptedVersions: Bool {
        !acceptedVersions.isEmpty && (installedRetailVersion == nil || installedVersionNotAccepted
                                      || targetVersion.map { !acceptedVersions.contains($0) } != false)
    }
    var canRetryCatalog: Bool { eligibility == .catalogUnavailable || eligibility == .catalogExpired }
    var reason: String? {
        switch eligibility {
        case .notListed: return L("Nextendo 目录未列出此游戏。")
        case .disabled: return L("Nextendo 目录已停用此游戏。")
        case .catalogUnavailable, .catalogExpired: return L("暂时无法验证 Nextendo 目录，请重试。")
        case .unsupportedCapabilities, .unsupportedPolicy: return L("此游戏需要当前 LumaNX 不支持的 Nextendo 功能。")
        case .versionNotAccepted: return L("Nextendo 不支持此游戏的待准备版本。")
        case .accepted, .versionUnknown:
            if let issue = game.onlinePreparation?.issues.first { return L(issue) }
            if game.packages.isEmpty { return L("添加 Nextendo 支持前，请先导入此游戏的源文件。") }
            if game.onlinePreparation == nil { return L("正在检查联机构建条件…") }
            return nil
        }
    }
    var canPrepare: Bool { reason == nil }
}

struct NextendoLibraryStatus: View {
    @EnvironmentObject var store: Store
    @ObservedObject var model: NextendoModel

    private var status: (label: String, symbol: String) {
        if model.busy || !model.checkedAccount { return ("Nextendo · 正在检查", "arrow.triangle.2.circlepath") }
        if model.message != nil || model.account?.hasNEXCredential == false {
            return ("Nextendo · 需要检查", "exclamationmark.circle")
        }
        guard model.account != nil else { return ("Nextendo · 未登录", "person.crop.circle") }
        guard store.preferences.onlineEngineDir != nil else { return ("Nextendo · 需要联机引擎", "gearshape") }
        let onlineReady = store.selected.map { $0.onlineReady == true }
            ?? store.games.contains { $0.onlineReady == true }
        return onlineReady ? ("Nextendo · 联机就绪", "checkmark.circle.fill") : ("Nextendo · 已登录", "network")
    }

    var body: some View {
        Button { store.selection = "settings" } label: {
            Label(L(status.label), systemImage: status.symbol)
        }
        .buttonStyle(.plain)
        .help(L("打开 Nextendo 设置"))
        .task { await model.refreshStatus() }
    }
}

struct NextendoPanel: View {
    @EnvironmentObject var store: Store
    @ObservedObject var model: NextendoModel
    var body: some View {
        GlassPanel(title: "Nextendo 联机（实验性）", symbol: "network") {
            HStack {
                TextField(L("客户端 ID（nxc_…）"), text: Binding(get: { store.preferences.nextendoClientID ?? "" }, set: { store.preferences.nextendoClientID = $0; store.savePreferences() }))
                    .textFieldStyle(.roundedBorder).disabled(model.busy)
                if model.signingIn { Button(L("取消登录"), action: model.cancel) }
                else if model.account == nil { Button(L("登录 Nextendo")) { model.signIn(clientID: store.preferences.nextendoClientID ?? "") }.disabled(model.busy) }
                else { Button(L("退出 Nextendo")) { Task { await model.signOut() } }.disabled(model.busy) }
            }.controlSize(.small)
            if let account = model.account {
                Label(account.identity?.username ?? "Nextendo", systemImage: account.hasNEXCredential ? "checkmark.circle" : "exclamationmark.triangle").font(.caption)
            }
            if let message = model.message { Text(L(message)).font(.caption).foregroundStyle(.orange) }
            Divider()
            HStack {
                Image(systemName: "folder").foregroundStyle(WorkspacePalette.accent)
                Text(store.preferences.onlineEngineDir.map { URL(fileURLWithPath: $0).lastPathComponent } ?? L("未选择")).lineLimit(1)
                Spacer()
                Button(L("选择联机引擎…"), action: store.chooseOnlineEngine)
                Button(L("准备联机引擎"), action: store.prepareOnlineEngine).disabled(!store.canManageToolchain || store.preferences.onlineEngineDir == nil)
            }.controlSize(.small)
            if !store.canManageToolchain { Text(L("请先退出游戏或等待当前任务结束，再准备联机引擎。")).font(.caption).foregroundStyle(.orange) }
            Text(L("联机版本使用独立引擎与运行文件，但与离线版本共用存档；每次准备前会自动备份。")).font(.caption).foregroundStyle(.secondary)
        }.task { await model.reload() }
    }
}
