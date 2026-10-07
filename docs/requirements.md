#CSVForge 

#Product Goals:

We are building an application where user can upload their CSV files, inspect it, analyse it, clean it and download the processed results  

#Problem 

Working with CSV often requires a repeatative task such as: 

- understanding the structure of the file 
- validating its content
- Removing duplicate rows 
- Removing whitespaces 
- Producing a processed copy 

#Target Users

A user who has a CSV file and wants to quickly inspect, clean, process and download it

An administrator should be able to view files, processing jobs and failures

#Core flows 

- Upload CSV
- Process CSV
- View Status
- View Results 
- Download Processed CSV


#Functional Requirements 


##Upload

The user should be able to upload a CSV file

##Analyse 

The system extracts:

File Name
File Size
Row Counts
Columns counts
Columns Names


##Validate

The system should detect any invalid CSV 


##Clean

The system can :

Trim whitespaces 
Detect duplicate rows 
Remove duplicate rows 

##Processing status : 

The user can see wheather processing is 

-queued
-processing
-completed
-failed 

##Download 

The user should be able to download the processed file 


#Non-functional Requirements 

The system should be 

-Secure
-Reliable 
-Observable
-Maintainable
-Resonably performant 

#Security Requirements 

- User must authenticate before accessing the protected functionality 

- user must only access the resources they are authorized to access 

- Uploaded files must not be publicly accessible 

- Secrets must not be commited to version control 

- CSV content must not be written to cell logs 

#Failure/Reliability Requirements 


-Invalid user input should not be retried indefinately
- temporary processing failure should be retryable 
- Duplicate processing request must not corrupt the state 
- Repeated worker failure should become observable

#MVP Scope

##Included 

- CSV Upload 
- CSV Analysis
- CSV Validation
- Whitespace removal
- Duplicate row detection and cleanup
- Processing status
- Processed file download
- Authentication 
- Admin file/job view

#Defination of Done 

The MVP is complete when user can 

- Log in 
- Upload a CSV 
- See the file being processed 
- See the final result 
- Download the processed copy 

The system should also handle input and processing failure safely 