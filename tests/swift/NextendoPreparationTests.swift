import XCTest
@testable import LumaNX
@testable import NextendoCore

final class NextendoPreparationTests: XCTestCase {
    private let title = "0100C2500FC20000"

    private func fixture(installed: String = "11.3.0", target: String? = "12.0.0") -> Game {
        let source = NSPPackage(titleId:title,baseId:title,version:2818048,type:"update",
                                name:"Splatoon 3",display:"12.0.0",path:"fixture",
                                size:1,sha256:"fixture",builds:[:])
        var game = Game(id:title,name:"Splatoon 3",display:installed,packages:[source],hidden:false,ready:true)
        game.runtimeRetailVersion = installed
        game.onlinePreparation = OnlinePreparation(version:target,issues:[])
        return game
    }

    private func catalog(expired: Bool = false) -> NextendoCatalogSnapshot {
        let now = Date()
        let policy = NextendoGamePolicy(titleIDs:[title.lowercased()],name:"Splatoon 3",enabled:true,
                                        protocolName:"npln",requiredCapabilities:["versions.exact.v1"],
                                        versionPolicy:.exact(["11.3.0","12.0.0"]),recommendedVersion:"12.0.0")
        let verified = NextendoVerifiedCatalog(revision:25,issuedAt:now.addingTimeInterval(-60),
                                               expiresAt:now.addingTimeInterval(expired ? -1 : 600),games:[policy])
        return NextendoCatalogSnapshot(catalog:verified,fetchedAt:now,refreshError:nil)
    }

    func testUnsupportedInstalledVersionCanPrepareAnAcceptedImportedUpdate() {
        let status = NextendoPreparationStatus(game:fixture(installed:"11.2.0"),catalog:catalog())
        XCTAssertTrue(status.canPrepare)
        XCTAssertEqual(status.installedVersion,"11.2.0")
        XCTAssertEqual(status.targetVersion,"12.0.0")
        XCTAssertTrue(status.installedVersionNotAccepted)
        XCTAssertTrue(status.showAcceptedVersions)
        XCTAssertEqual(status.acceptedVersions,["11.3.0","12.0.0"])
    }

    func testIneligibleBuildOrTitleCannotPrepare() {
        let incompatible = NextendoPreparationStatus(game:fixture(target:"11.2.0"),catalog:catalog())
        XCTAssertEqual(incompatible.eligibility,.versionNotAccepted(["11.3.0","12.0.0"]))
        XCTAssertFalse(incompatible.canPrepare)
        XCTAssertTrue(incompatible.showAcceptedVersions)
        var unlisted = fixture()
        unlisted.id = "0400D4201C6EA000"
        let status = NextendoPreparationStatus(game:unlisted,catalog:catalog())
        XCTAssertEqual(status.eligibility,.notListed)
        XCTAssertFalse(status.canPrepare)
        XCTAssertNil(status.targetVersion)
    }

    func testMissingSourcesBlockPreparationAndUnknownDerivedVersionCanBeCheckedByWorker() {
        var missing = fixture()
        missing.packages = []
        let blocked = NextendoPreparationStatus(game:missing,catalog:catalog())
        XCTAssertEqual(blocked.eligibility,.accepted)
        XCTAssertFalse(blocked.canPrepare)
        let unknownVersion = NextendoPreparationStatus(game:fixture(target:nil),catalog:catalog())
        XCTAssertEqual(unknownVersion.eligibility,.versionUnknown)
        XCTAssertTrue(unknownVersion.canPrepare)
    }

    func testUnavailableAndExpiredCatalogsCanBeRetried() {
        for snapshot in [nil, catalog(expired:true)] {
            let status = NextendoPreparationStatus(game:fixture(),catalog:snapshot)
            XCTAssertFalse(status.canPrepare)
            XCTAssertTrue(status.canRetryCatalog)
            XCTAssertTrue(status.acceptedVersions.isEmpty)
        }
    }
}
