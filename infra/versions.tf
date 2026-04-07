terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.43"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  backend "azurerm" {
    resource_group_name  = "rg-tfstate-dev"
    storage_account_name = "stmvptfstate"
    container_name       = "tfstate"
    key                  = "pipeline-resolver.tfstate"
  }
}

provider "azurerm" {
  features {}
}
