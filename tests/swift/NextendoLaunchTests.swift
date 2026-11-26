import NextendoCore
import XCTest
@testable import LumaNX

@MainActor final class NextendoLaunchTests: XCTestCase {
    private struct FixtureCredentials: NextendoCredentialStore {
        let session: NextendoSession?
        func load() throws -> NextendoSession? { session }
        func save(_ session: NextendoSession) throws { XCTFail("Launch must not replace this fixture's credential") }
        func remove() throws { XCTFail("Launch must not remove this fixture's credential") }
    }

    func testLaunchAutomaticallyUsesNextendoWhenSignedInAndOnlineReady() async throws {
        try await checkLaunch(signedIn:true,onlineReady:true,expectedOnline:true)
    }

    func testLaunchFallsBackToOfflineWhenSignedOutOrOnlineVersionIsUnavailable() async throws {
        try await checkLaunch(signedIn:false,onlineReady:true,expectedOnline:false)
        try await checkLaunch(signedIn:true,onlineReady:false,expectedOnline:false)
    }

    func testPlayerCanExplicitlyLaunchOfflineWhileSignedIn() async throws {
        try await checkLaunch(signedIn:true,onlineReady:true,forceOffline:true,expectedOnline:false)
    }

    private func checkLaunch(signedIn:Bool,onlineReady:Bool,forceOffline:Bool=false,expectedOnline:Bool) async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at:root) }
        let resources = root.appendingPathComponent("resources")
        let worker = resources.appendingPathComponent("Worker")
        let bin = resources.appendingPathComponent("Runtime/python/bin")
        try FileManager.default.createDirectory(at:worker,withIntermediateDirectories:true)
        try FileManager.default.createDirectory(at:bin,withIntermediateDirectories:true)
        try FileManager.default.createSymbolicLink(atPath:bin.appendingPathComponent("python3").path,
                                                   withDestinationPath:"/usr/bin/python3")
        try #"""
import json, pathlib, sys
request = json.loads(pathlib.Path(sys.argv[2]).read_text())
if request["operation"] == "launch":
    online = request.get("network") == "nextendo"
    received = bool(sys.stdin.buffer.read(1)) if request.get("credentialPipe") else False
    result = {"online": online, "receivedCredential": received}
    pathlib.Path(request["privateRoot"], "launch.json").write_text(json.dumps(result))
print(json.dumps(dict(schema=1, taskId=request["taskId"], type="result", state="completed", result={})), flush=True)
"""#.write(to:worker.appendingPathComponent("main.py"),atomically:true,encoding:.utf8)

        let now = Date()
        let session = NextendoSession(tokenKind:.nex,token:"fixture-token",identity:NextendoIdentity(pid:42),
                                      scopes:["game.matchmaking"],createdAt:now,expiresAt:now.addingTimeInterval(3600))
        let model = NextendoModel(service:NextendoAccountService(store:FixtureCredentials(session:signedIn ? session : nil)))
        let store = Store(privateRoot:root.appendingPathComponent("private"),resources:resources,nextendo:model)
        defer { store.terminate() }
        store.checks = [ToolCheck(name:"fixture",ok:true,message:"")]
        store.environmentReportComplete = true
        store.preferences.onlineEngineDir = root.appendingPathComponent("online-engine").path
        var game = Game(id:"0100C2500FC20000",name:"Splatoon 3",display:"12.0.0",packages:[],hidden:false,ready:true)
        game.onlineReady = onlineReady
        store.launch(game,offline:forceOffline)

        let deadline = Date().addingTimeInterval(5)
        while (store.executionBusy || !store.processes.isEmpty), Date() < deadline {
            try await Task.sleep(for:.milliseconds(20))
        }
        XCTAssertFalse(store.executionBusy)
        let data = try Data(contentsOf:store.privateRoot.appendingPathComponent("launch.json"))
        let result = try XCTUnwrap(JSONSerialization.jsonObject(with:data) as? [String:Bool])
        XCTAssertEqual(result["online"],expectedOnline)
        XCTAssertEqual(result["receivedCredential"],expectedOnline)
    }
}
