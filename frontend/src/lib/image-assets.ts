const ASSET_URL = /^\/api\/assets\/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/?$/i;

export const EDITOR_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
/**
 * The current asset API decodes at most 12 MP and 8,192 px per side. Keep
 * these values in the client as a compatibility target: high-resolution
 * camera images can be resized before they reach both current and older AKB
 * backends instead of failing even when their compressed file is under 1 MB.
 */
export const EDITOR_IMAGE_MAX_PIXELS = 12_000_000;
export const EDITOR_IMAGE_MAX_DIMENSION = 8_192;
export const EDITOR_IMAGE_MIME_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
] as const;

export interface PreparedEditorImage {
  file: File;
  optimized: boolean;
  originalWidth?: number;
  originalHeight?: number;
  width?: number;
  height?: number;
}

export interface EditorImageUploadFailure {
  message: string;
  retryable: boolean;
}

export class EditorImagePreparationError extends Error {
  readonly retryable = false;

  constructor(message: string) {
    super(message);
    this.name = "EditorImagePreparationError";
  }
}

export function assetIdFromUrl(src: string | null | undefined): string | null {
  if (!src) return null;
  return ASSET_URL.exec(src.trim())?.[1]?.toLowerCase() ?? null;
}

export function validateEditorImage(file: File): string | null {
  if (!EDITOR_IMAGE_MIME_TYPES.includes(file.type as (typeof EDITOR_IMAGE_MIME_TYPES)[number])) {
    return "Choose a PNG, JPEG, GIF, or WebP image.";
  }
  if (file.size > EDITOR_IMAGE_MAX_BYTES) {
    return `${formatBytes(file.size)} selected — ${formatBytes(file.size - EDITOR_IMAGE_MAX_BYTES)} over the 10 MB limit.`;
  }
  return null;
}

export function fitEditorImageDimensions(
  width: number,
  height: number,
): { width: number; height: number } {
  const pixelScale = Math.sqrt(EDITOR_IMAGE_MAX_PIXELS / (width * height));
  const dimensionScale = EDITOR_IMAGE_MAX_DIMENSION / Math.max(width, height);
  const scale = Math.min(1, pixelScale, dimensionScale);
  return {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  };
}

function canvasBlob(
  canvas: HTMLCanvasElement,
  type: string,
  quality?: number,
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob) resolve(blob);
        else reject(new EditorImagePreparationError("The optimized image could not be encoded."));
      },
      type,
      quality,
    );
  });
}

/**
 * Resize compressed high-resolution still images to the asset API's decode
 * boundary. A 16 MP JPEG can be well below 1 MB, so byte validation alone is
 * not enough. GIFs remain untouched to avoid silently dropping animation.
 * Browsers without createImageBitmap fall back to server-side validation.
 */
export async function prepareEditorImage(file: File): Promise<PreparedEditorImage> {
  const validationMessage = validateEditorImage(file);
  if (validationMessage) throw new EditorImagePreparationError(validationMessage);
  if (typeof createImageBitmap !== "function") return { file, optimized: false };

  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new EditorImagePreparationError(
      "This image could not be read. Choose a valid PNG, JPEG, GIF, or WebP file.",
    );
  }

  try {
    const originalWidth = bitmap.width;
    const originalHeight = bitmap.height;
    const fitted = fitEditorImageDimensions(originalWidth, originalHeight);
    if (fitted.width === originalWidth && fitted.height === originalHeight) {
      return {
        file,
        optimized: false,
        originalWidth,
        originalHeight,
        width: originalWidth,
        height: originalHeight,
      };
    }

    const resolution = `${originalWidth.toLocaleString()}×${originalHeight.toLocaleString()}`;
    if (file.type === "image/gif") {
      throw new EditorImagePreparationError(
        `${resolution} animated image exceeds the 12 MP limit. Resize it before uploading to preserve its animation.`,
      );
    }

    const canvas = document.createElement("canvas");
    canvas.width = fitted.width;
    canvas.height = fitted.height;
    const context = canvas.getContext("2d");
    if (!context) {
      throw new EditorImagePreparationError(
        "This browser could not optimize the high-resolution image. Resize it below 12 MP and try again.",
      );
    }
    context.drawImage(bitmap, 0, 0, fitted.width, fitted.height);
    const blob = await canvasBlob(
      canvas,
      file.type,
      file.type === "image/jpeg" || file.type === "image/webp" ? 0.9 : undefined,
    );
    if (blob.size > EDITOR_IMAGE_MAX_BYTES) {
      throw new EditorImagePreparationError(
        `The optimized image is ${formatBytes(blob.size)}. Resize it below 10 MB and try again.`,
      );
    }

    return {
      file: new File([blob], file.name, {
        type: file.type,
        lastModified: file.lastModified,
      }),
      optimized: true,
      originalWidth,
      originalHeight,
      width: fitted.width,
      height: fitted.height,
    };
  } finally {
    bitmap.close();
  }
}

export function classifyEditorImageUploadFailure(
  error: unknown,
  file?: File,
): EditorImageUploadFailure {
  if (error instanceof EditorImagePreparationError) {
    return { message: error.message, retryable: false };
  }

  const status =
    typeof error === "object" && error !== null && "status" in error
      ? Number((error as { status?: unknown }).status)
      : 0;
  const serverMessage = error instanceof Error ? error.message : "The image could not be uploaded.";
  const name = file?.name ? `${file.name}: ` : "";

  if (status === 413) {
    return {
      message: `${name}the server rejected this image because it exceeds 10 MB, 12 MP, or 8,192 pixels per side. Choose another image or resize it and try again.`,
      retryable: false,
    };
  }
  if (status === 415) {
    return {
      message: `${name}the file is not a valid supported image. Choose a PNG, JPEG, GIF, or WebP image.`,
      retryable: false,
    };
  }
  if (status === 403) {
    return {
      message: `${name}you do not have permission to add images to this Vault.`,
      retryable: false,
    };
  }
  if (status === 409) {
    return { message: `${name}${serverMessage}`, retryable: false };
  }
  if (status === 408) {
    return {
      message: `${name}the upload timed out before it finished. Try again.`,
      retryable: true,
    };
  }
  if (status === 429) {
    return {
      message: `${name}image uploads are busy right now. Wait a moment and try again.`,
      retryable: true,
    };
  }
  if (status >= 500) {
    return {
      message: `${name}the server could not store the image right now. Try again.`,
      retryable: true,
    };
  }

  return {
    message: `${name}${serverMessage}`,
    retryable: status === 0,
  };
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
