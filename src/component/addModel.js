import React, { useEffect, useState, useRef } from 'react';
import { Button } from 'primereact/button';
import { InputText } from 'primereact/inputtext';
import { Card } from 'primereact/card';
import { Toast } from 'primereact/toast';
import { ListBox } from 'primereact/listbox';
import { Dialog } from 'primereact/dialog';
import { ProgressSpinner } from 'primereact/progressspinner';
import axios from 'axios';

// Utility function to convert bytes to a human-readable format
const formatSize = (bytes) => {
    if (bytes === 0 || !bytes) return 'N/A';
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(2)} ${sizes[i]}`;
};

const AddModel = () => {
    const [modelName, setModelName] = useState('');
    const [selectedOllama, setSelectedOllama] = useState(localStorage.getItem("selectedOllama") || 'http://195.230.127.227:11800');
    const [installedModels, setInstalledModels] = useState([]);
    const [selectedModel, setSelectedModel] = useState(null);
    const [loading, setLoading] = useState(false);
    const [showConfirmDialog, setShowConfirmDialog] = useState(false);
    const toast = useRef(null);

    // Fetch installed models on component mount
    useEffect(() => {
        fetchInstalledModels();
    }, []);

    // Fetch installed models from the API
    const fetchInstalledModels = async () => {
        setLoading(true);
        try {
            const response = await axios.get(`${selectedOllama}/api/tags`);
            const models = response.data.models.map((model) => ({
                label: model.name, // Display the model name
                value: model.name, // Use the model name as the value
                details: model,    // Store the full model object for details
            }));
            setInstalledModels(models);
        } catch (err) {
            console.error('Failed to fetch installed models:', err);
            toast.current?.show({ severity: 'error', summary: 'Error', detail: 'Failed to fetch installed models', life: 3000 });
        } finally {
            setLoading(false);
        }
    };

    // Handle pulling a new model
    const handlePull = async () => {
        if (!modelName.trim()) {
            toast.current?.show({ severity: 'warn', summary: 'Warning', detail: 'Please enter a model name', life: 3000 });
            return;
        }

        setLoading(true);
        try {
            await axios.post(`${selectedOllama}/api/pull`, { name: modelName });
            toast.current?.show({ severity: 'success', summary: 'Success', detail: `Model "${modelName}" pulled successfully`, life: 3000 });
            fetchInstalledModels(); // Refresh the list of installed models
            setModelName(''); // Clear the input field
        } catch (err) {
            console.error('Failed to pull model:', err);
            toast.current?.show({ severity: 'error', summary: 'Error', detail: 'Failed to pull model', life: 3000 });
        } finally {
            setLoading(false);
        }
    };

    // Handle removing a selected model
    const handleRemove = async () => {
        setShowConfirmDialog(false); // Close the confirmation dialog
        setLoading(true);
        try {
            await axios.delete(`${selectedOllama}/api/delete`, {
                data: { model: selectedModel }, // Pass the model name in the request body
            });
            toast.current?.show({ severity: 'success', summary: 'Success', detail: `Model "${selectedModel}" removed successfully`, life: 3000 });
            fetchInstalledModels(); // Refresh the list of installed models
        } catch (err) {
            console.error('Failed to remove model:', err);
            toast.current?.show({ severity: 'error', summary: 'Error', detail: 'Failed to remove model', life: 3000 });
        } finally {
            setLoading(false);
        }
    };

    return (
        <div style={{ display: 'flex', gap: '20px', padding: '20px' }}>
            {/* Left Section: Installed Models */}
            <div style={{ flex: 1 }}>
                <Card title="Installed Models" style={{ height: '100%' }}>
                    {loading ? (
                        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '300px' }}>
                            <ProgressSpinner />
                        </div>
                    ) : (
                        <>
                            <ListBox
                                value={selectedModel}
                                options={installedModels} // Already formatted with label, value, and details
                                onChange={(e) => setSelectedModel(e.value)}
                                style={{ height: '300px' }}
                                listStyle={{ maxHeight: '300px' }}
                            />
                            <div style={{ marginTop: '10px', display: 'flex', justifyContent: 'space-between' }}>
                                <Button
                                    label="Remove"
                                    icon="pi pi-trash"
                                    className="p-button-danger"
                                    onClick={() => setShowConfirmDialog(true)}
                                    disabled={!selectedModel}
                                />
                                <Button
                                    label="Refresh"
                                    icon="pi pi-refresh"
                                    className="p-button-secondary"
                                    onClick={fetchInstalledModels}
                                />
                            </div>
                        </>
                    )}
                    {/* Model Details Section */}
                    {selectedModel && (
                        <div style={{ marginTop: '20px', padding: '10px', border: '1px solid #ddd', borderRadius: '6px' }}>
                            <h4>Model Details</h4>
                            <p><strong>Name:</strong> {selectedModel}</p>
                            <p><strong>Size:</strong> {formatSize(installedModels.find((m) => m.value === selectedModel)?.details.size)}</p>
                            <p><strong>Last Modified:</strong> {installedModels.find((m) => m.value === selectedModel)?.details.modified_at || 'N/A'}</p>
                            <p><strong>Digest:</strong> {installedModels.find((m) => m.value === selectedModel)?.details.digest || 'N/A'}</p>
                        </div>
                    )}
                </Card>
            </div>

            {/* Right Section: Add Model */}
            <div style={{ flex: 2 }}>
                <Card title="Add Model" style={{ textAlign: 'center' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', justifyContent: 'center', marginBottom: '20px' }}>
                        <InputText
                            value={modelName}
                            onChange={(e) => setModelName(e.target.value)}
                            placeholder="Enter model name"
                            style={{ width: '70%' }}
                        />
                        <Button label="Pull" icon="pi pi-download" onClick={handlePull} />
                        <Button label="Cancel" icon="pi pi-times" className="p-button-secondary" onClick={() => setModelName('')} />
                    </div>
                </Card>
            </div>

            {/* Confirmation Dialog */}
            <Dialog
                visible={showConfirmDialog}
                onHide={() => setShowConfirmDialog(false)}
                header="Confirm Deletion"
                footer={
                    <div>
                        <Button label="No" icon="pi pi-times" onClick={() => setShowConfirmDialog(false)} className="p-button-text" />
                        <Button label="Yes" icon="pi pi-check" onClick={handleRemove} className="p-button-danger" />
                    </div>
                }
            >
                <p>Are you sure you want to remove the model "{selectedModel}"?</p>
            </Dialog>

            <Toast ref={toast} />
        </div>
    );
};

export default AddModel;