// 컷 사진의 글자를 Mac 내장 글자 인식(Vision)으로 읽는다 — 설치·결제 없음.
// 사용: cut_ocr 파일1 파일2 ...  →  {"파일1": "읽은 글자\n...", ...} JSON 한 줄
import Foundation
import Vision

var out: [String: String] = [:]
for path in CommandLine.arguments.dropFirst() {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["ko-KR", "en-US"]
    req.usesLanguageCorrection = true
    do {
        try VNImageRequestHandler(url: URL(fileURLWithPath: path)).perform([req])
        out[path] = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    } catch {
        out[path] = ""
    }
}
let data = try JSONSerialization.data(withJSONObject: out, options: [])
print(String(data: data, encoding: .utf8)!)
