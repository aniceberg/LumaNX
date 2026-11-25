import AppKit
import NextendoCore
import SwiftUI

/// Account state for optional Nextendo play. Tokens stay inside NextendoCore's
/// Keychain store; only the public client ID is kept in preferences.
@MainActor final class NextendoModel: ObservableObject {
    static let scopes = ["identity", "friends", "game.matchmaking", "presence"]
    @Published private(set) var account: NextendoAccountSummary?
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
    /// Renews the 24-hour game token when needed.
    func reload() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
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
        do { try await service.signOut(); account = nil; message = nil } catch { message = Self.describe(error) }
    }
    /// Transport errors can contain request URLs, so only typed errors are shown.
    static func describe(_ error: Error) -> String {
        (error as? NextendoError)?.errorDescription ?? "Nextendo 登录未完成，请重试。"
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
