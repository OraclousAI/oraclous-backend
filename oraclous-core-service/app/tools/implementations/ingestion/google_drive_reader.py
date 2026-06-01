from typing import Any, Dict
import io
import pandas as pd
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from decimal import Decimal

from app.tools.base.auth_tool import OAuthTool
from app.tools.plugin import CapabilityKindPlugin, plugin_registry
from app.tools.registry import tool_registry
from app.models.capability_descriptor import DescriptorKind
from app.schemas.tool_instance import ExecutionContext, ExecutionResult
from app.schemas.tool_definition import (
    ToolDefinition,
    ToolSchema,
    ToolCapability,
    CredentialRequirement,
)
from app.schemas.common import ToolCategory, ToolType, CredentialType
from app.utils.tool_id_generator import generate_tool_id


class GoogleDriveReader(OAuthTool, CapabilityKindPlugin):
    """
    Tool for reading files from Google Drive
    Supports various file types: CSV, Excel, Docs, Sheets, etc.
    Now supports multiple operations including listing files
    """

    @classmethod
    def get_tool_definition(cls) -> ToolDefinition:
        """Return the tool definition for Google Drive Reader"""
        return ToolDefinition(
            id=generate_tool_id("Google Drive Reader", "1.0.0", "INGESTION"),
            name="Google Drive Reader",
            description="Read and extract data from Google Drive files including Sheets, Docs, CSV, and Excel files",
            version="1.0.0",
            category=ToolCategory.INGESTION,
            type=ToolType.INTERNAL,
            capabilities=[
                ToolCapability(
                    name="read_drive_files",
                    description="Read content from specific Google Drive files",
                ),
                ToolCapability(
                    name="list_drive_files",
                    description="List files in Google Drive folders",
                ),
                ToolCapability(
                    name="extract_spreadsheet_data",
                    description="Extract data from Google Sheets",
                ),
                ToolCapability(
                    name="download_files",
                    description="Download files from Google Drive",
                ),
            ],
            tags=["google", "drive", "spreadsheet", "document", "cloud", "ingestion"],
            input_schema=ToolSchema(
                type="object",
                properties={
                    # ENHANCED: Add operation type
                    "operation": {
                        "type": "string",
                        "enum": ["read_file", "list_files"],
                        "description": "Type of operation to perform",
                        "default": "read_file",
                    },
                    # File reading parameters
                    "file_id": {
                        "type": "string",
                        "description": "Google Drive file ID (required for read_file operation)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to file in Google Drive (alternative to file_id)",
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": "Sheet name for Google Sheets files (optional)",
                    },
                    "range": {
                        "type": "string",
                        "description": "Cell range for Google Sheets (e.g., 'A1:Z100')",
                    },
                    "include_headers": {
                        "type": "boolean",
                        "description": "Whether to include headers (default: true)",
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["auto", "sheets", "csv", "excel", "docs", "pdf"],
                        "description": "File type to process (auto-detect if not specified)",
                    },
                    # File listing parameters
                    "folder_id": {
                        "type": "string",
                        "description": "Google Drive folder ID to list files from (optional for list_files)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query to filter files (optional for list_files)",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Number of files to return (max 100, default 10)",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 10,
                    },
                },
                required=["operation"],
                description="Input parameters for Google Drive operations",
            ),
            output_schema=ToolSchema(
                type="object",
                properties={
                    "operation": {
                        "type": "string",
                        "description": "Operation that was performed",
                    },
                    "data": {
                        "type": "array",
                        "description": "Extracted data rows or file list",
                    },
                    "headers": {
                        "type": "array",
                        "description": "Column headers or file metadata fields",
                    },
                    "file_info": {
                        "type": "object",
                        "description": "File metadata information (for read_file)",
                    },
                    "row_count": {
                        "type": "integer",
                        "description": "Number of data rows or files",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Additional operation metadata",
                    },
                },
                description="Results from Google Drive operation",
            ),
            credential_requirements=[
                CredentialRequirement(
                    type=CredentialType.OAUTH_TOKEN,
                    provider="google",
                    required=True,
                    scopes=["https://www.googleapis.com/auth/drive.readonly"],
                    description="Google OAuth token with Drive read access",
                )
            ],
        )

    async def _execute_internal(
        self, input_data: Dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        """Enhanced execution with operation routing"""
        try:
            # Get OAuth credentials
            oauth_creds = self.get_credentials(context, "OAUTH_TOKEN")
            access_token = oauth_creds["access_token"]

            # Build Google Drive service
            from google.oauth2.credentials import Credentials

            credentials = Credentials(token=access_token)

            # Route based on operation type
            operation = input_data.get("operation", "read_file")

            if operation == "list_files":
                result_data = await self._list_drive_files(input_data, credentials)
                result_data["operation"] = "list_files"

            elif operation == "read_file":
                if not input_data.get("file_id"):
                    raise ValueError("file_id is required for read_file operation")

                result_data = await self._read_drive_file(input_data, credentials)
                result_data["operation"] = "read_file"

            else:
                raise ValueError(f"Unsupported operation: {operation}")

            return ExecutionResult(
                success=True,
                data=result_data,
                metadata={
                    "operation": operation,
                    "processing_time": "calculated_later",
                },
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                error_message=f"Failed to execute Google Drive operation: {str(e)}",
            )

    async def _read_drive_file(
        self, input_data: Dict[str, Any], credentials
    ) -> Dict[str, Any]:
        """Read content from a specific Google Drive file - UPDATED for PDF"""
        drive_service = build("drive", "v3", credentials=credentials)

        file_id = input_data["file_id"]
        file_type = input_data.get("file_type", "auto")

        # Get file metadata
        file_metadata = (
            drive_service.files()
            .get(fileId=file_id, fields="id,name,mimeType,size,modifiedTime")
            .execute()
        )

        # Determine processing method based on file type
        if file_type == "auto":
            file_type = self._detect_file_type(file_metadata["mimeType"])

        # Process based on file type
        if file_type == "sheets":
            result_data = await self._process_google_sheets(
                file_id, input_data, credentials
            )
        elif file_type in ["csv", "excel"]:
            result_data = await self._process_file_download(
                drive_service, file_id, file_type, input_data
            )
        elif file_type == "docs":
            result_data = await self._process_google_docs(
                file_id, input_data, credentials
            )
        elif file_type == "pdf":
            result_data = await self._process_pdf_file(
                drive_service, file_id, input_data
            )
        elif file_type == "unknown":
            # ADDED: Better error for unknown files
            raise ValueError(
                f"Unsupported file type: {file_metadata['mimeType']}. "
                f"Supported types: Google Docs, Google Sheets, CSV, Excel, PDF"
            )
        else:
            raise ValueError(f"Unsupported file type: {file_type}")

        # Add file metadata
        result_data["file_info"] = {
            "id": file_metadata["id"],
            "name": file_metadata["name"],
            "mime_type": file_metadata["mimeType"],
            "size": file_metadata.get("size"),
            "modified_time": file_metadata["modifiedTime"],
        }

        return result_data

    async def _list_drive_files(
        self, input_data: Dict[str, Any], credentials
    ) -> Dict[str, Any]:
        """
        List files in a Google Drive folder.
        Enhanced version with better parameter handling
        """
        drive_service = build("drive", "v3", credentials=credentials)

        folder_id = input_data.get("folder_id")
        query = input_data.get("query")
        page_size = input_data.get("page_size", 10)

        # Build query for files().list()
        q_parts = []

        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")

        if query:
            q_parts.append(query)

        # Default: exclude trashed files
        q_parts.append("trashed=false")

        query_str = " and ".join(q_parts) if q_parts else "trashed=false"

        results = (
            drive_service.files()
            .list(
                q=query_str,
                pageSize=min(page_size, 100),  # Cap at 100
                fields="files(id, name, mimeType, modifiedTime, size, parents, webViewLink)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )

        files = results.get("files", [])

        # Format data as table
        headers = ["id", "name", "mimeType", "size", "modifiedTime", "webViewLink"]
        data = []

        for f in files:
            data.append(
                [
                    f["id"],
                    f["name"],
                    f["mimeType"],
                    f.get("size", "N/A"),
                    f["modifiedTime"],
                    f.get("webViewLink", ""),
                ]
            )

        return {
            "data": data,
            "headers": headers,
            "row_count": len(data),
            "metadata": {
                "operation": "list_files",
                "folder_id": folder_id,
                "query": query,
                "total_found": len(files),
            },
        }

    def _detect_file_type(self, mime_type: str) -> str:
        """Detect file type from MIME type"""
        mime_mapping = {
            "application/vnd.google-apps.spreadsheet": "sheets",
            "application/vnd.google-apps.document": "docs",
            "text/csv": "csv",
            "application/vnd.ms-excel": "excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
            "application/pdf": "pdf",
            "image/png": "image",
            "image/jpeg": "image",
            "text/plain": "text",
        }
        return mime_mapping.get(mime_type, "unknown")

    async def _process_pdf_file(
        self, drive_service, file_id: str, input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process PDF file by downloading it
        Note: PDF text extraction would require additional libraries like PyPDF2
        For now, we'll return file metadata and download info
        """
        try:
            # Get file metadata first
            file_metadata = (
                drive_service.files()
                .get(fileId=file_id, fields="id,name,size,modifiedTime,webViewLink")
                .execute()
            )

            # For PDFs, we can provide metadata and download info
            # Full text extraction would require PyPDF2 or similar
            return {
                "data": [
                    ["property", "value"],
                    ["file_name", file_metadata["name"]],
                    ["file_size", file_metadata.get("size", "N/A")],
                    ["modified_time", file_metadata["modifiedTime"]],
                    ["download_url", f"https://drive.google.com/file/d/{file_id}/view"],
                    ["web_view_link", file_metadata.get("webViewLink", "")],
                ],
                "headers": ["property", "value"],
                "row_count": 5,
                "metadata": {
                    "file_type": "pdf",
                    "note": "PDF text extraction requires additional processing",
                },
            }

        except Exception as e:
            raise Exception(f"Failed to process PDF file: {str(e)}")

    async def _process_pdf_with_text_extraction(
        self, drive_service, file_id: str, input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process PDF file with text extraction (requires PyPDF2)
        """
        try:
            import PyPDF2

            # Download PDF file
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)

            done = False
            while done is False:
                status, done = downloader.next_chunk()

            fh.seek(0)

            # Extract text from PDF
            pdf_reader = PyPDF2.PdfReader(fh)
            text_content = []

            for page_num, page in enumerate(pdf_reader.pages):
                page_text = page.extract_text()
                if page_text.strip():
                    text_content.append([f"Page {page_num + 1}", page_text.strip()])

            return {
                "data": text_content,
                "headers": ["page", "content"],
                "row_count": len(text_content),
                "metadata": {
                    "file_type": "pdf",
                    "total_pages": len(pdf_reader.pages),
                    "text_extracted": True,
                },
            }

        except ImportError:
            # Fallback to metadata-only if PyPDF2 not available
            return await self._process_pdf_file(drive_service, file_id, input_data)
        except Exception as e:
            raise Exception(f"Failed to extract text from PDF: {str(e)}")

    async def _process_google_sheets(
        self, file_id: str, input_data: Dict[str, Any], credentials
    ) -> Dict[str, Any]:
        """Process Google Sheets file"""
        sheets_service = build("sheets", "v4", credentials=credentials)

        sheet_name = input_data.get("sheet_name")
        cell_range = input_data.get("range", "")
        include_headers = input_data.get("include_headers", True)

        # Build range string
        range_string = sheet_name if sheet_name else "Sheet1"
        if cell_range:
            range_string += f"!{cell_range}"

        # Get spreadsheet data
        result = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=file_id, range=range_string)
            .execute()
        )

        values = result.get("values", [])
        if not values:
            return {"data": [], "headers": [], "row_count": 0}

        headers = []
        data = values

        if include_headers and values:
            headers = values[0]
            data = values[1:]

        return {"data": data, "headers": headers, "row_count": len(data)}

    async def _process_file_download(
        self, drive_service, file_id: str, file_type: str, input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Process downloadable files (CSV, Excel)"""
        # Download file content
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while done is False:
            status, done = downloader.next_chunk()

        fh.seek(0)

        # Process based on file type
        if file_type == "csv":
            df = pd.read_csv(fh)
        elif file_type == "excel":
            df = pd.read_excel(fh)
        else:
            raise ValueError(f"Unsupported file type for download: {file_type}")

        include_headers = input_data.get("include_headers", True)

        if include_headers:
            headers = df.columns.tolist()
            data = df.values.tolist()
        else:
            headers = []
            data = df.values.tolist()

        return {"data": data, "headers": headers, "row_count": len(data)}

    async def _process_google_docs(
        self, file_id: str, input_data: Dict[str, Any], credentials
    ) -> Dict[str, Any]:
        """Process Google Docs file (extract text content)"""
        docs_service = build("docs", "v1", credentials=credentials)

        # Get document content
        document = docs_service.documents().get(documentId=file_id).execute()

        # Extract text content
        content = []
        for element in document.get("body", {}).get("content", []):
            if "paragraph" in element:
                paragraph = element["paragraph"]
                for text_element in paragraph.get("elements", []):
                    if "textRun" in text_element:
                        content.append(text_element["textRun"]["content"])

        full_text = "".join(content)

        # Split into lines for structured output
        lines = [line.strip() for line in full_text.split("\n") if line.strip()]

        return {
            "data": [[line] for line in lines],  # Each line as a row
            "headers": ["content"],
            "row_count": len(lines),
        }

    def calculate_credits(self, input_data: Any, result: ExecutionResult) -> Decimal:
        if not result.success or not result.data:
            return Decimal("0.1")

        row_count = result.data.get("row_count", 0)
        base_credits = Decimal("0.1")
        row_credits = Decimal(str(row_count)) * Decimal("0.001")

        return base_credits + row_credits

    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "google-drive-reader",
            "version": {"hash": "", "tags": ["1.0.0"]},
            "metadata": {
                "name": "Google Drive Reader",
                "description": "Read and extract data from Google Drive files including Sheets, Docs, CSV, and Excel files",
            },
            "spec": {
                "implementation": {
                    "type": "internal",
                    "handler": "app.tools.implementations.ingestion.google_drive_reader.GoogleDriveReader",
                },
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
                "credential_requirements": [
                    {"type": "oauth2", "required": True, "provider": "google"},
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "google-drive-reader"


plugin_registry.register(GoogleDriveReader)
tool_registry.register_tool(GoogleDriveReader)
