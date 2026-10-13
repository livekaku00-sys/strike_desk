import { createContext, useContext } from 'react';

// Backend state polled once by DashboardLayout and shared with the header, sidebar,
// footer and alert matrix, so each consumer does not run its own copy of the poll.
// Shape: { execMode, classLock, risk } — each { status, value, updatedAt }.
// null outside DashboardLayout (e.g. the guest page), where hooks poll for themselves.
export const SystemStateContext = createContext(null);

export const useSystemState = () => useContext(SystemStateContext);
