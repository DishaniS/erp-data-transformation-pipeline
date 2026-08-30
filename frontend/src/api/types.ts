/**
 * Response types for the two upload endpoints this frontend uses.
 *
 * Taken from `artifacts/phase13_openapi.json`, which FastAPI generates from the
 * running application. The backend returns more than the UI displays; only the
 * fields this screen can show are typed here.
 */

export interface CsvUploadResponse {
  upload_id: string;
  filename: string;
  content_hash: string;
  size_bytes: number;
  schema_id?: string | null;
  columns: number;
  /** Data rows actually inspected during schema inference. Always present. */
  rows_sampled: number;
  /** True when inference stopped at its ceiling, so the file holds more rows. */
  sample_limited?: boolean;
  /**
   * Total data rows. `null` means NOT COUNTED - schema inference samples rather
   * than counting, so this stays null on the upload path. Never display it as a
   * row total when it is null, and never substitute `rows_sampled` for it.
   */
  rows_observed?: number | null;
  warnings: string[];
}

export interface DocumentUploadResponse {
  upload_id: string;
  filename: string;
  content_hash: string;
  size_bytes: number;
  /** Content-addressed. Identical bytes always yield the same id. */
  document_id?: string | null;
  file_type?: string | null;
  page_count: number;
  extraction_status?: string | null;
  /** True only when OCR actually produced text on at least one page. */
  ocr_used: boolean;
  /**
   * Phase 6. The indexing job this upload started, and that job's status as
   * the response was written - not a promise about its outcome. Poll
   * `GET /v1/jobs/{index_job_id}` for the authoritative lifecycle.
   *
   * Both null means indexing did not start; `indexing_error` says why.
   */
  index_job_id?: string | null;
  indexing_status?: string | null;
  /** Set only when the upload succeeded and indexing could not be started. */
  indexing_error?: string | null;
  warnings: string[];
}
