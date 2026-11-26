import Foundation
import AppKit

@MainActor extension Store {
    func archiveGame(_ game:Game) {
        guard !executionBusy,queue.isEmpty,game.ready==true else {return}
        let panel=NSOpenPanel()
        panel.title=L("选择 NAS 归档文件夹")
        panel.prompt=L("选择")
        panel.canChooseDirectories=true;panel.canChooseFiles=false;panel.canCreateDirectories=true
        if let root=preferences.archiveRoot {panel.directoryURL=URL(fileURLWithPath:root)}
        guard panel.runModal() == .OK,let url=panel.url else {return}
        let alert=NSAlert();alert.messageText=L("归档已编译游戏？")
        alert.informativeText=L("将复制并校验已编译游戏，然后移除本地运行文件以释放空间。存档和本地游戏缓存保留。恢复时不需要重新编译。")+"\n\n"+url.path
        alert.addButton(withTitle:L("归档到 NAS"));alert.addButton(withTitle:L("取消"))
        guard alert.runModal() == .alertFirstButtonReturn else {return}
        preferences.archiveRoot=url.path;savePreferences()
        runtimeTransfer("archiveGame",game:game,extra:["archiveRoot":url.path])
    }
    func publishPlayableGame(_ game: Game) {
        guard !executionBusy,queue.isEmpty,game.ready==true,game.playingFromNAS != true else { return }
        let panel = NSOpenPanel()
        panel.title = L("选择 NAS 游玩文件夹")
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        if let root = preferences.archiveRoot { panel.directoryURL = URL(fileURLWithPath:root) }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard (try? url.resourceValues(forKeys:[.volumeIsLocalKey]).volumeIsLocal) == false else {
            error = "请选择已挂载的 NAS 共享文件夹。"
            return
        }
        let alert = NSAlert()
        alert.messageText = L("移至 NAS 并直接游玩")
        alert.informativeText = L("校验后移除本地运行文件。游戏直接从 NAS 加载资源。存档、启动程序副本和构建缓存保留在 Mac。") + "\n\n" + url.path
        let option = NSButton(checkboxWithTitle:sourceTransferLabel(game),target:nil,action:nil)
        option.state = .off
        option.sizeToFit()
        if (game.localManagedSourceSize ?? 0) > 0 { alert.accessoryView = option }
        alert.addButton(withTitle:L("移至 NAS 并直接游玩"))
        alert.addButton(withTitle:L("取消"))
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        runtimeTransfer("publishNAS",game:game,extra:["archiveRoot":url.path,"includeManagedSources":option.state == .on])
    }
    private func sourceTransferLabel(_ game:Game) -> String {
        let size = ByteCountFormatter.string(fromByteCount:game.localManagedSourceSize ?? 0,countStyle:.file)
        return String(format:L("包括托管源文件（%@）"),size)
    }
    func publishManagedSources(_ game:Game) {
        guard !executionBusy,queue.isEmpty,game.playingFromNAS==true,(game.localManagedSourceSize ?? 0)>0 else {return}
        let alert=NSAlert()
        alert.messageText=L("将托管源文件移至 NAS")
        alert.informativeText=sourceTransferLabel(game)+"\n"+L("未来的构建将在 Mac 上运行，并从此游戏已登记的 NAS 目录读取源文件。")
        alert.addButton(withTitle:L("将托管源文件移至 NAS"))
        alert.addButton(withTitle:L("取消"))
        guard alert.runModal() == .alertFirstButtonReturn else {return}
        runtimeTransfer("publishNASSources",game:game)
    }
    func moveNASGameBackToMac(_ game: Game) {
        guard game.playingFromNAS == true, !executionBusy, queue.isEmpty else { return }
        runtimeTransfer("restoreNAS",game:game)
    }
    func restoreGame(_ game:Game) {
        guard game.archived==true else {return}
        runtimeTransfer("restoreGame",game:game)
    }
    func runtimeTransfer(_ operation:String,game:Game,extra:[String:Any]=[:]) {
        guard !executionBusy,queue.isEmpty,["archiveGame","restoreGame","publishNAS","restoreNAS","publishNASSources"].contains(operation) else {return}
        let libraryId=preferences.libraryId
        var request=extra;request["gameId"]=game.id
        selection="tasks"
        let id=worker(operation,extra:request) { _ in
            guard self.preferences.libraryId==libraryId else {return}
            if operation == "publishNASSources" { self.notice = "托管源文件已移至 NAS。" }
            else if operation == "publishNAS" { self.notice = "游戏已移至 NAS，可直接启动。" }
            else { self.notice=operation=="archiveGame" ? "游戏已归档到 NAS；存档保留在本地。" : "游戏已恢复到本地，无需重新编译；NAS 副本保留。" }
            self.refresh()
        }
        executionTask=id
        if let index=tasks.firstIndex(where:{$0.id==id}) {
            tasks[index].archiveRoot=request["archiveRoot"] as? String
            tasks[index].includeManagedSources=request["includeManagedSources"] as? Bool
        }
    }
}
