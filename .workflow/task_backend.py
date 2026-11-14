class TaskBackend:
    PLUGGABLE = ["farmtable", "github", "jira", "gitlab", "beads", "linear"]
    def __init__(self, backend_type):
        self.backend_type = backend_type
