import Foundation
import Testing
@testable import Cagentic

/// How tool activity reads in the transcript.
struct ToolLabelTests {
    @Test("A single call shows the gateway's phrase, not the identifier")
    func showsThePhrase() {
        let activity = AssistantActivity.tool(
            id: "t1",
            name: "list_dir",
            summary: "/Users/mete",
            label: "Listed directory",
            labelPlural: "Listed {n} directories"
        )

        #expect(activity.displayLabel(count: 1) == "Listed directory")
    }

    @Test("A run of the same tool collapses into a count")
    func collapsesARun() {
        let activity = AssistantActivity.tool(
            id: "t1",
            name: "list_dir",
            summary: "/Users/mete",
            label: "Listed directory",
            labelPlural: "Listed {n} directories"
        )

        // The case this exists for: four identical rows saying nothing the count would not.
        #expect(activity.displayLabel(count: 4) == "Listed 4 directories")
        #expect(activity.displayLabel(count: 2) == "Listed 2 directories")
    }

    @Test("An older gateway that sends no phrasing still renders")
    func fallsBackToTheName() {
        let activity = AssistantActivity.tool(id: "t1", name: "list_dir", summary: "")

        // Not pretty, but a raw name beats an empty row.
        #expect(activity.displayLabel(count: 1) == "list_dir")
        #expect(activity.displayLabel(count: 3) == "list_dir ×3")
    }

    @Test("A phrase without a plural form still counts")
    func fallsBackWhenOnlySingularIsKnown() {
        let activity = AssistantActivity.tool(
            id: "t1",
            name: "odd_tool",
            summary: "",
            label: "Did something"
        )

        #expect(activity.displayLabel(count: 2) == "Did something ×2")
    }

    @Test("The phrasing arrives with the tool call")
    func decodesTheLabels() throws {
        let events = try GatewayEventDecoder().decode(
            frame: #"""
            {"kind":"tool_call","data":{"id":"t1","name":"list_dir","summary":"/tmp",
            "label":"Listed directory","label_plural":"Listed {n} directories"}}
            """#
        )

        guard case .toolCall(let call) = try #require(events.first) else {
            Issue.record("Expected a tool call")
            return
        }
        #expect(call.label == "Listed directory")
        #expect(call.labelPlural == "Listed {n} directories")
    }

    @Test("Stored history carries the phrasing as well")
    func decodesLabelsFromStoredHistory() throws {
        let payload = #"""
        {"id":"abc","title":"T","model":"m","messages":[
          {"role":"assistant","content":"done","tool_details":[
            {"name":"read_file","label":"Read file","label_plural":"Read {n} files",
             "summary":"notes.txt","ok":true,"first_line":"ok"}]}]}
        """#
        let detail = try #require(
            try JSONDecoder()
                .decode(GatewayChatDetailPayload.self, from: Data(payload.utf8))
                .value
        )

        let tool = try #require(detail.messages.first?.tools.first)
        // A chat reopened later has to read the same as one watched live.
        #expect(tool.label == "Read file")
        #expect(tool.labelPlural == "Read {n} files")
    }
}
