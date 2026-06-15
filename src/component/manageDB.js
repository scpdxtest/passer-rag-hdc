import React, { useState, useEffect, useCallback } from 'react';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { Button } from 'primereact/button';
import { InputText } from 'primereact/inputtext';
import { Card } from 'primereact/card';
import { Dialog } from 'primereact/dialog';
import { ConfirmDialog, confirmDialog } from 'primereact/confirmdialog';
import { Toast } from 'primereact/toast';
import { ProgressSpinner } from 'primereact/progressspinner';
import { FilterMatchMode } from 'primereact/api';
import { ChromaClient } from 'chromadb';
import './manageDB.css'; // Updated CSS file for custom styles

const ManageDB = () => {
    const [databases, setDatabases] = useState([]);
    const [loading, setLoading] = useState(false);
    const [selectedDatabase, setSelectedDatabase] = useState(null);
    const [dialogVisible, setDialogVisible] = useState(false);
    const [newDatabaseName, setNewDatabaseName] = useState('');
    const [editMode, setEditMode] = useState(false);
    const [chromaUrl, setChromaUrl] = useState(localStorage.getItem('selectedChromaDB') || 'http://127.0.0.1:8000');
    const toast = React.useRef(null);

    const [filters, setFilters] = useState({
        global: { value: null, matchMode: FilterMatchMode.CONTAINS },
        name: { value: null, matchMode: FilterMatchMode.STARTS_WITH },
    });

    // Fetch databases from ChromaDB
    const fetchDatabases = useCallback(async () => {
        setLoading(true);
        try {
            const client = new ChromaClient({ path: chromaUrl });
            const collections = await client.listCollections();
            const formattedDatabases = collections.map((name, index) => ({
                id: index + 1,
                name,
                createdAt: new Date().toLocaleDateString(), // Placeholder; adjust if you have real timestamps
                status: 'Active', // Placeholder; adjust based on your logic
            }));
            setDatabases(formattedDatabases);
            toast.current.show({ severity: 'success', summary: 'Success', detail: 'Databases loaded successfully' });
        } catch (error) {
            console.error('Error fetching databases:', error);
            toast.current.show({ severity: 'error', summary: 'Error', detail: 'Failed to load databases' });
        } finally {
            setLoading(false);
        }
    }, [chromaUrl]);

    useEffect(() => {
        fetchDatabases();
    }, [fetchDatabases]);

    // Add or Edit Database
    const handleSaveDatabase = async () => {
        if (!newDatabaseName.trim()) {
            toast.current.show({ severity: 'warn', summary: 'Warning', detail: 'Database name is required' });
            return;
        }

        setLoading(true);
        try {
            const client = new ChromaClient({ path: chromaUrl });
            if (editMode) {
                // Logic for editing (if supported by ChromaDB)
                // For now, just refresh
                await fetchDatabases();
                toast.current.show({ severity: 'success', summary: 'Success', detail: 'Database updated' });
            } else {
                // Create new collection
                await client.createCollection({ name: newDatabaseName });
                await fetchDatabases();
                toast.current.show({ severity: 'success', summary: 'Success', detail: 'Database created successfully' });
            }
            setDialogVisible(false);
            setNewDatabaseName('');
            setEditMode(false);
        } catch (error) {
            console.error('Error saving database:', error);
            toast.current.show({ severity: 'error', summary: 'Error', detail: 'Failed to save database' });
        } finally {
            setLoading(false);
        }
    };

    // Delete Database
    const handleDeleteDatabase = async (database) => {
        confirmDialog({
            message: `Are you sure you want to delete "${database.name}"? This action cannot be undone.`,
            header: 'Confirm Deletion',
            icon: 'pi pi-exclamation-triangle',
            accept: async () => {
                setLoading(true);
                try {
                    const client = new ChromaClient({ path: chromaUrl });
                    await client.deleteCollection(database.name);
                    await fetchDatabases();
                    toast.current.show({ severity: 'success', summary: 'Success', detail: 'Database deleted successfully' });
                } catch (error) {
                    console.error('Error deleting database:', error);
                    toast.current.show({ severity: 'error', summary: 'Error', detail: 'Failed to delete database' });
                } finally {
                    setLoading(false);
                }
            },
        });
    };

    // Action buttons for DataTable
    const actionBodyTemplate = (rowData) => {
        return (
            <div className="manage-db-action-buttons">
                <Button
                    icon="pi pi-pencil"
                    className="p-button-rounded p-button-text p-button-info"
                    onClick={() => {
                        setSelectedDatabase(rowData);
                        setNewDatabaseName(rowData.name);
                        setEditMode(true);
                        setDialogVisible(true);
                    }}
                    tooltip="Edit Database"
                    tooltipOptions={{ position: 'top' }}
                />
                <Button
                    icon="pi pi-trash"
                    className="p-button-rounded p-button-text p-button-danger"
                    onClick={() => handleDeleteDatabase(rowData)}
                    tooltip="Delete Database"
                    tooltipOptions={{ position: 'top' }}
                />
            </div>
        );
    };

    // Status template for DataTable
    const statusBodyTemplate = (rowData) => {
        return (
            <span className={`manage-db-status-badge ${rowData.status === 'Active' ? 'active' : 'inactive'}`}>
                {rowData.status}
            </span>
        );
    };

    return (
        <div className="manage-db-container">
            <Toast ref={toast} />
            <ConfirmDialog />

            {/* Compact Header */}
            <div className="manage-db-header">
                <h2 className="manage-db-header-title">
                    <i className="pi pi-database"></i> Manage Databases
                </h2>
                <p className="manage-db-header-subtitle">Manage your ChromaDB collections</p>
                <Button
                    label="Add Database"
                    icon="pi pi-plus"
                    className="p-button-primary manage-db-add-button"
                    onClick={() => {
                        setNewDatabaseName('');
                        setEditMode(false);
                        setDialogVisible(true);
                    }}
                />
            </div>

            {/* Configuration Section */}
            <div className="manage-db-config">
                <h4 className="manage-db-section-title">
                    <i className="pi pi-cog"></i> Configuration
                </h4>
                <div className="manage-db-config-row">
                    <InputText
                        value={chromaUrl}
                        onChange={(e) => {
                            setChromaUrl(e.target.value);
                            localStorage.setItem('selectedChromaDB', e.target.value);
                        }}
                        placeholder="http://127.0.0.1:8000"
                        className="manage-db-input"
                        style={{ flex: 1 }}
                    />
                    <Button
                        label="Refresh"
                        icon="pi pi-refresh"
                        className="p-button-secondary manage-db-refresh-button"
                        onClick={fetchDatabases}
                        loading={loading}
                        tooltip="Refresh database list"
                        tooltipOptions={{ position: 'top' }}
                    />
                </div>
            </div>

            {/* Databases Table */}
            <div className="manage-db-table">
                <h4 className="manage-db-section-title">
                    <i className="pi pi-list"></i> Databases
                </h4>
                {loading ? (
                    <div className="manage-db-loading">
                        <ProgressSpinner />
                        <p>Loading...</p>
                    </div>
                ) : (
                    <DataTable
                        className="manage-db-datatable"
                        value={databases}
                        filters={filters}
                        filterDisplay="row"
                        paginator
                        rows={10}
                        rowsPerPageOptions={[5, 10, 25]}
                        selection={selectedDatabase}
                        onSelectionChange={(e) => setSelectedDatabase(e.value)}
                        selectionMode="single"
                        size="small"
                        showGridlines
                        stripedRows
                        scrollable
                        scrollHeight="300px"
                        emptyMessage="No databases found."
                        style={{ width: '100%' }}
                    >
                        <Column field="name" header="Name" filter filterPlaceholder="Search" sortable />
                        <Column field="createdAt" header="Created" sortable />
                        <Column field="status" header="Status" body={statusBodyTemplate} sortable />
                        <Column header="Actions" body={actionBodyTemplate} style={{ width: '120px' }} />
                    </DataTable>
                )}
            </div>

            {/* Add/Edit Dialog */}
            <Dialog
                className="manage-db-dialog"
                header={
                    <div className="manage-db-dialog-header">
                        <i className={editMode ? "pi pi-pencil" : "pi pi-plus"}></i>
                        {editMode ? "Edit Database" : "Add New Database"}
                    </div>
                }
                visible={dialogVisible}
                style={{ width: '400px' }}
                modal
                onHide={() => setDialogVisible(false)}
                footer={
                    <div className="manage-db-dialog-footer">
                        <Button
                            label="Cancel"
                            icon="pi pi-times"
                            className="p-button-text"
                            onClick={() => setDialogVisible(false)}
                        />
                        <Button
                            label={editMode ? "Update" : "Create"}
                            icon="pi pi-check"
                            className="p-button-primary"
                            onClick={handleSaveDatabase}
                            loading={loading}
                        />
                    </div>
                }
            >
                <div className="manage-db-dialog-content">
                    <label className="manage-db-dialog-label">Database Name:</label>
                    <InputText
                        value={newDatabaseName}
                        onChange={(e) => setNewDatabaseName(e.target.value)}
                        placeholder="Enter database name"
                        className="manage-db-dialog-input"
                    />
                </div>
            </Dialog>
        </div>
    );
};

export default ManageDB;