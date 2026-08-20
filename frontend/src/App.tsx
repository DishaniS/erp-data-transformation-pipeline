import { UploadPage } from "./pages/Upload";

/**
 * One screen, no router.
 *
 * This frontend exists to put files into the pipeline. Everything else the
 * backend can do stays available over HTTP but is deliberately not surfaced
 * here, so there is nothing to navigate between.
 */
export default function App() {
  return <UploadPage />;
}
