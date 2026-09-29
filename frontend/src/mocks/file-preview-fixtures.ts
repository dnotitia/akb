export const FILE_PREVIEW_YAML_TEXT =
  "# Synthetic YAML preview\nservice:\n  name: 'fixture: unchanged'\n  tags:\n    - teal\n    - amber\n";

export const FILE_PREVIEW_INVALID_YAML_TEXT =
  "# Malformed YAML remains readable\nservice:\n  labels: [unfinished\n";

export type FilePreviewFixture = {
  id: string;
  name: string;
  mime_type: string | null;
  preview_kind: "Text" | "JSON" | "Image" | "PDF";
  raw_text: string | null;
};

export const FILE_PREVIEW_CASES: FilePreviewFixture[] = [
  {
    id: "yaml-application",
    name: "service.yaml",
    mime_type: "application/yaml",
    preview_kind: "Text",
    raw_text: FILE_PREVIEW_YAML_TEXT,
  },
  {
    id: "yaml-text",
    name: "legacy.yml",
    mime_type: "text/yaml",
    preview_kind: "Text",
    raw_text: FILE_PREVIEW_YAML_TEXT,
  },
  {
    id: "yaml-missing",
    name: "missing.yaml",
    mime_type: null,
    preview_kind: "Text",
    raw_text: FILE_PREVIEW_YAML_TEXT,
  },
  {
    id: "yaml-generic",
    name: "generic.yml",
    mime_type: "application/octet-stream",
    preview_kind: "Text",
    raw_text: FILE_PREVIEW_YAML_TEXT,
  },
  {
    id: "yaml-malformed",
    name: "malformed.yaml",
    mime_type: "application/yaml",
    preview_kind: "Text",
    raw_text: FILE_PREVIEW_INVALID_YAML_TEXT,
  },
  {
    id: "yaml-explicit-json",
    name: "explicit-format.yaml",
    mime_type: "application/json",
    preview_kind: "JSON",
    raw_text: "{\n  \"format\": \"json\"\n}\n",
  },
  {
    id: "existing-json",
    name: "settings.json",
    mime_type: "application/json",
    preview_kind: "JSON",
    raw_text: "{\n  \"enabled\": true\n}\n",
  },
  {
    id: "existing-image",
    name: "diagram.png",
    mime_type: "image/png",
    preview_kind: "Image",
    raw_text: null,
  },
  {
    id: "existing-pdf",
    name: "reference.pdf",
    mime_type: "application/pdf",
    preview_kind: "PDF",
    raw_text: null,
  },
];

export const FILE_PREVIEW_PUBLICATION = {
  slug: "file-preview-yaml-publication",
  name: "public-settings.yaml",
  mime_type: "application/yaml",
  raw_text: FILE_PREVIEW_YAML_TEXT,
};

export function filePreviewDiscovery(origin: string) {
  const ordinary = FILE_PREVIEW_CASES[0];
  const ordinaryUrl = origin + "/vault/fixture/file/" + ordinary.id;
  const publicUrl = origin + "/p/" + FILE_PREVIEW_PUBLICATION.slug;

  return {
    identity: {
      user_id: "u-file-preview-reader",
      vault: "fixture",
      file_id: ordinary.id,
      file_uri: "akb://fixture/file/" + ordinary.id,
      file_path: ordinary.name,
      start_url: ordinaryUrl,
      actors: ["reader-file-preview"],
    },
    operations: {
      state: { method: "GET", url: origin + "/__akb_mock__/fixture/state" },
    },
    file_preview: {
      ordinary: {
        entry_url: ordinaryUrl,
        name: ordinary.name,
        mime_type: ordinary.mime_type,
        expected_text: ordinary.raw_text,
      },
      public: {
        entry_url: publicUrl,
        name: FILE_PREVIEW_PUBLICATION.name,
        mime_type: FILE_PREVIEW_PUBLICATION.mime_type,
        expected_text: FILE_PREVIEW_PUBLICATION.raw_text,
      },
      cases: FILE_PREVIEW_CASES.map((file) => ({
        id: file.id,
        name: file.name,
        mime_type: file.mime_type,
        preview_kind: file.preview_kind,
        entry_url: origin + "/vault/fixture/file/" + file.id,
        expected_text: file.raw_text,
      })),
    },
  };
}
