import Foundation

struct WorkspaceConfig: Codable {
    let version: Int
    let projects: [WorkspaceProject]
    let clis: [WorkspaceCli]
    let general: [WorkspaceGeneral]
}

struct WorkspaceProject: Codable {
    let id: String
    let label: String
    let icon: String
    let color: String
    let active: Bool?
    let actions: [WorkspaceAction]

    var isActive: Bool { active ?? true }
}

struct WorkspaceAction: Codable {
    let label: String
    let prompt: String
    let steps: [WorkspaceStep]?
}

struct WorkspaceCli: Codable {
    let id: String
    let label: String
    let prompt: String
    let steps: [WorkspaceStep]?
}

struct WorkspaceStep: Codable {
    let field: String
    let prompt: String
    let required: Bool
}

struct WorkspaceGeneral: Codable {
    let label: String
    let prompt: String
}
