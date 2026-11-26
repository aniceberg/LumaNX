import XCTest
@testable import LumaNX

final class TransferRateTests: XCTestCase {
    private func task(_ operation: String = "publishNAS", stage: String = "Copy playable runtime to NAS") -> TaskRecord {
        var task = TaskRecord(id:"t",operation:operation,state:"running")
        task.stage = stage; task.unit = "B"
        return task
    }
    private func feed(_ task: inout TaskRecord, _ samples: [(Double, Double)]) {
        for (time, done) in samples { task.done = done; task.sampleTransfer(now:time) }
    }

    func testWriteSpeedIsMeasuredInDecimalMegabytes() {
        var copy = task()
        feed(&copy, [(0, 0), (1, 34_000_000)])
        XCTAssertEqual(copy.transferRateText, L("写入")+" 34 MB/s")
    }

    func testVerifyingAndCopyingBackReadFromTheNAS() {
        var verify = task(stage:"Verify playable NAS runtime")
        feed(&verify, [(0, 0), (2, 16_800_000)])
        XCTAssertEqual(verify.transferRateText, L("读取")+" 8.4 MB/s")
        var back = task("restoreNAS", stage:"Copy playable runtime back to Mac")
        feed(&back, [(0, 0), (1, 40_000_000)])
        XCTAssertEqual(back.transferRateText, L("读取")+" 40 MB/s")
    }

    func testSpeedIsSmoothedAndSamplesUnderASecondApartAreSkipped() {
        var copy = task()
        feed(&copy, [(0, 0), (1, 10_000_000), (1.5, 50_000_000), (2, 30_000_000)])
        // 10 MB/s, then 20 MB/s over the next second: 0.7*10 + 0.3*20 = 13.
        XCTAssertEqual(copy.transferRateText, L("写入")+" 13 MB/s")
    }

    func testANewStageStartsOverAndOtherTasksShowNoSpeed() {
        var copy = task()
        feed(&copy, [(0, 0), (1, 34_000_000)])
        copy.resetTransfer()
        XCTAssertNil(copy.transferRateText)
        var build = task("build", stage:"Copy playable runtime to NAS")
        feed(&build, [(0, 0), (1, 34_000_000)])
        XCTAssertNil(build.transferRateText)
        var files = task(); files.unit = "files"
        feed(&files, [(0, 0), (1, 34)])
        XCTAssertNil(files.transferRateText)
    }
}
