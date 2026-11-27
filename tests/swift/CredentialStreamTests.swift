import XCTest
@testable import LumaNX

/// Must match Worker/online.py (CREDENTIAL_STREAM, first_credential_pipe) and dm/host/hle_nextendo_apple.m.
@MainActor final class CredentialStreamTests: XCTestCase {
    func testStreamStartsWithTheSharedMagic() {
        XCTAssertEqual(Store.credentialStreamMagic, Data("NXCRED1\n".utf8))
    }

    func testFramesCarryABigEndianLengthThenTheCredential() {
        let credential = Data(repeating: 0x41, count: 0x01_02)
        let frame = Store.credentialFrame(credential)
        XCTAssertEqual(Array(frame.prefix(4)), [0, 0, 1, 2])
        XCTAssertEqual(frame.dropFirst(4), credential)
    }

    func testRenewalLeavesTheAccessTokenAtLeastTwentyMinutes() {
        // Each frame carries a token with at least 50 minutes (NextendoModel.launchAccessLifetime).
        XCTAssertLessThanOrEqual(Store.credentialRenewalInterval, .seconds(NextendoModel.launchAccessLifetime - 10 * 60))
    }
}
